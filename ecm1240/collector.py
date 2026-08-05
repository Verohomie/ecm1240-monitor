#!/usr/bin/env python3
"""ECM-1240 collector service — the system of record.

  1. Reads packets from one or more serial ports (or a TCP gateway).
  2. Decodes them and derives per-channel watts from the watt-second counters
     (wrap- and reset-guarded).
  3. Writes readings to a local SQLite database in batched transactions.
  4. Optionally re-serves the live values as CSV lines on a TCP port, for a
     home-automation controller to consume.

The read side (live values + history as JSON) is a separate service, api.py,
over the same database.

WHY IDENTITY COMES FROM THE PORT, NOT THE PACKET
------------------------------------------------
Every ECM-1240 reports `unit_id=0` and `serial=0` in its real-time packets
regardless of how it is configured, and no command exists to change it. The
field is useless for telling two meters apart.

So identity is bound to the PHYSICAL PORT: each meter is declared in the config
with its own `port:` and its own `unit:`, and the decoded unit_id is discarded
in favour of the configured one.

Bind to `/dev/serial/by-path/...` names, NOT `/dev/ttyUSB0`. Cheap PL2303-style
adapters report no serial number, so `/dev/serial/by-id/` names collide, and
ttyUSB ordering is not stable across reboots. If two meters swap, the collector
will subtract one meter's counters from the other's and emit confidently wrong
watts with no error and no visible symptom. `tools/verify_unit_identity.py`
exists to catch exactly this.

Run:  python3 -m ecm1240.collector [--config PATH] [--dry-run]
"""

import argparse
import json
import os
import queue
import socket
import sqlite3
import threading
import time

from . import config as cfgmod
from .protocol import (CHANNELS, PACKET_LEN, checksum_ok, framed, parse_payload,
                       watts)

IDLE_TIMEOUT = 120          # s without gateway bytes -> drop socket, re-accept
SPOOL_CAP = 20_000          # readings buffered if a flush fails (~1 day at 5 s)

log_lock = threading.Lock()


def log(msg):
    with log_lock:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def raw_dump(d, dsecs, dwall, w):
    """Raw packet fields, for diagnosing a bad interval from the journal."""
    dwall_s = f"{dwall:.2f}s" if dwall is not None else "?"
    return (f"secs={d['secs']} dsecs={dsecs} dwall={dwall_s} "
            f"ch1_aws={d['ch1_aws']} ch2_aws={d['ch2_aws']} "
            f"ch1_amps={d['ch1_amps']:.2f} ch2_amps={d['ch2_amps']:.2f} "
            f"volts={d['volts']:.1f} flag=0x{d['flag']:02x} "
            f"raw_ch1={w['ch1']:.0f}W raw_ch2={w['ch2']:.0f}W aux_ws={d['aux_ws']}")


class LiveFeed:
    """Broadcasts one CSV line per packet to any connected TCP client.

    Intended for a home-automation controller that can open a socket and parse
    a line. Disabled unless `collector.live_feed_port` is set.

        E,<unit>,<volts*10>,<ch1_W>,<ch2_W>,<aux1_W>,...,<aux5_W>\\r\\n
    """

    def __init__(self, port, bind="127.0.0.1"):
        self.port = port
        self.bind = bind
        self.clients = []
        self.lock = threading.Lock()
        if port:
            threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(4)
        log(f"live CSV feed listening on {self.bind}:{self.port}")
        while True:
            sock, addr = srv.accept()
            log(f"live feed client connected: {addr[0]}")
            with self.lock:
                self.clients.append(sock)

    def broadcast(self, line):
        if not self.port:
            return
        data = (line + "\r\n").encode("ascii")
        with self.lock:
            alive = []
            for sock in self.clients:
                try:
                    sock.sendall(data)
                    alive.append(sock)
                except OSError:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    log("live feed client dropped")
            self.clients = alive


class SqliteStore:
    """Buffers readings and commits them in batches.

    WAL plus one transaction per flush, so the storage device sees a couple of
    writes a minute rather than one fsync per sample. WAL also lets a separate
    reader process (the API) run concurrently without blocking the writer.
    """

    # `dsecs` is how many seconds of energy this row's watts are the average OF:
    # the meter's own seconds-counter delta, which is the number watts() divided
    # by. It is NOT the gap between timestamps — ts is stamped when the packet
    # lands, so it also contains USB/serial latency. The two differ most where it
    # matters least forgivably: an ECM-1240 slips to a short interval right at the
    # moment a large load switches, and a consumer that assumes a fixed cadence
    # then over-weights exactly that reading. Storing it means no consumer has to
    # guess.
    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS readings ("
        " ts INTEGER NOT NULL, unit_id INTEGER NOT NULL, volts REAL,"
        " ch1 REAL, ch2 REAL, aux1 REAL, aux2 REAL, aux3 REAL, aux4 REAL, aux5 REAL,"
        " dsecs INTEGER,"
        " PRIMARY KEY (ts, unit_id)) WITHOUT ROWID"
    )  # PK(ts,unit_id) doubles as the history-range index (ts leads)

    # Columns added after the first release. Applied on open, guarded by a
    # PRAGMA check so it is idempotent: an existing database picks the column up
    # on the next restart and keeps every row it already has. Rows written before
    # the column existed keep NULL there, and readers fall back to the nominal
    # cadence for those — which reproduces the old plain-average result exactly,
    # rather than silently re-weighting history that cannot be re-derived.
    MIGRATIONS = (
        ("readings", "dsecs", "ALTER TABLE readings ADD COLUMN dsecs INTEGER"),
    )

    # Audit log of intervals the consistency guards threw out, so the app can
    # surface how often each rule fires. The pattern over time is what tells
    # you whether a sensor fault is getting better or worse.
    SCHEMA_DISCARDS = (
        "CREATE TABLE IF NOT EXISTS discards ("
        " ts INTEGER NOT NULL, unit_id INTEGER, reason TEXT,"
        " ch1_w REAL, limit_w REAL)"
    )

    # Quarantine keeps the FULL rejected packet, not just its mains value. A
    # branch-sum rejection has already passed the coherence (V*A) check, so it
    # is electrically real — typically a large unmetered load — and can be
    # promoted back into `readings` later. The discards table (ch1 only)
    # cannot support that.
    SCHEMA_QUARANTINE = (
        "CREATE TABLE IF NOT EXISTS quarantine ("
        " ts INTEGER NOT NULL, unit_id INTEGER, reason TEXT, volts REAL,"
        " ch1 REAL, ch2 REAL, aux1 REAL, aux2 REAL, aux3 REAL, aux4 REAL, aux5 REAL,"
        " limit_w REAL)"
    )

    def __init__(self, db_path, flush_interval, dry_run):
        self.db_path = db_path
        self.flush_interval = flush_interval
        self.dry_run = dry_run
        self.buf = []
        self.discards = []
        self.quarantine = []
        self.lock = threading.Lock()
        if not dry_run:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        threading.Thread(target=self._loop, daemon=True).start()

    def _append(self, target, rec):
        with self.lock:
            target.append(rec)
            if len(target) > SPOOL_CAP:
                del target[: len(target) - SPOOL_CAP]

    def add(self, reading):
        self._append(self.buf, reading)

    def add_discard(self, rec):
        self._append(self.discards, rec)

    def add_quarantine(self, rec):
        self._append(self.quarantine, rec)

    def _loop(self):
        db = None
        if not self.dry_run:
            db = sqlite3.connect(self.db_path, timeout=10)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(self.SCHEMA)
            db.execute(self.SCHEMA_DISCARDS)
            db.execute(self.SCHEMA_QUARANTINE)
            for table, col, ddl in self.MIGRATIONS:
                have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    db.execute(ddl)
                    log(f"schema: added {table}.{col}")
            db.commit()
            log(f"sqlite store open: {self.db_path} "
                f"(WAL, flush every {self.flush_interval}s)")
        while True:
            time.sleep(self.flush_interval)
            with self.lock:
                batch, self.buf = self.buf, []
                dbatch, self.discards = self.discards, []
                qbatch, self.quarantine = self.quarantine, []
            if not batch and not dbatch and not qbatch:
                continue
            if self.dry_run:
                if batch:
                    log(f"dry-run: would store {len(batch)} readings "
                        f"(newest ts={batch[-1]['ts']})")
                if dbatch:
                    log(f"dry-run: would store {len(dbatch)} discards")
                if qbatch:
                    log(f"dry-run: would store {len(qbatch)} quarantined")
                continue
            try:
                if batch:
                    db.executemany(
                        "INSERT OR REPLACE INTO readings"
                        " (ts,unit_id,volts,ch1,ch2,aux1,aux2,aux3,aux4,aux5,dsecs)"
                        " VALUES (:ts,:unit_id,:volts,:ch1,:ch2,:aux1,:aux2,:aux3,"
                        ":aux4,:aux5,:dsecs)", batch)
                if dbatch:
                    db.executemany(
                        "INSERT INTO discards (ts,unit_id,reason,ch1_w,limit_w)"
                        " VALUES (:ts,:unit_id,:reason,:ch1_w,:limit_w)", dbatch)
                if qbatch:
                    db.executemany(
                        "INSERT INTO quarantine"
                        " (ts,unit_id,reason,volts,ch1,ch2,aux1,aux2,aux3,aux4,aux5,"
                        "limit_w) VALUES (:ts,:unit_id,:reason,:volts,:ch1,:ch2,:aux1,"
                        ":aux2,:aux3,:aux4,:aux5,:limit_w)", qbatch)
                db.commit()
            except sqlite3.Error as e:
                log(f"sqlite write failed ({e}); re-buffering "
                    f"{len(batch)}+{len(dbatch)}+{len(qbatch)}")
                with self.lock:
                    self.buf = batch + self.buf
                    self.discards = dbatch + self.discards
                    self.quarantine = qbatch + self.quarantine


# ── Byte sources ──────────────────────────────────────────────────────────────

def gateway_source(port, bind="0.0.0.0"):
    """Yield byte chunks from whichever gateway connection is current."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(1)
    log(f"gateway server listening on {bind}:{port} — waiting for it to connect")
    while True:
        sock, addr = srv.accept()
        log(f"gateway connected from {addr[0]}:{addr[1]}")
        sock.settimeout(IDLE_TIMEOUT)
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    log("gateway closed the connection; re-accepting")
                    break
                yield data
        except socket.timeout:
            log(f"no gateway data for {IDLE_TIMEOUT}s; dropping half-open socket")
        except OSError as e:
            log(f"gateway socket error: {e}")
        finally:
            try:
                sock.close()
            except OSError:
                pass


def tcp_source(target):
    host, _, port = target.rpartition(":")
    while True:
        try:
            sock = socket.create_connection((host, int(port)), timeout=10)
            sock.settimeout(IDLE_TIMEOUT)
            log(f"connected to {target}")
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                yield data
        except OSError as e:
            log(f"{target}: {e}; retrying in 10s")
        time.sleep(10)


def serial_source(port, baud):
    """Yield byte chunks from a serial port, reopening if it goes away.

    One unplugged meter must not take the other port's logging down with it.
    """
    import serial  # pyserial, only needed for the serial path
    while True:
        conn = None
        try:
            conn = serial.Serial(port, baud, timeout=1)
            log(f"reading {port} @ {baud} 8N1")
            while True:
                data = conn.read(256)
                if data:
                    yield data
        except OSError as e:  # serial.SerialException subclasses OSError
            log(f"{port}: {e}; retrying in 10s")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        time.sleep(10)


def pump(label, unit, source, q, counts):
    """Frame one source in its own thread and hand packets to the decode loop."""
    def on_bad():
        counts["bad"] += 1
        log(f"{label}: bad checksum #{counts['bad']}")
    for packet in framed(source, on_bad):
        q.put((unit, packet))


# ── Main ──────────────────────────────────────────────────────────────────────

def build_sources(cfg, args):
    sources = []
    if args.tcp:
        sources.append((args.tcp, None, tcp_source(args.tcp)))
        return sources
    if args.gateway:
        port = cfg["collector"].get("gateway_port", 8082)
        sources.append(("gateway", None, gateway_source(port)))
        return sources
    for m in cfgmod.meters(cfg):
        dev, unit = m["port"], m["unit"]
        baud = m.get("baud", 19200)
        sources.append((dev, unit, serial_source(dev, baud)))
    return sources


def main():
    ap = argparse.ArgumentParser(description="ECM-1240 collector service")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--tcp", metavar="HOST:PORT",
                    help="read from a TCP gateway instead of the configured serial ports")
    ap.add_argument("--gateway", action="store_true",
                    help="listen for an inbound gateway connection instead of serial")
    ap.add_argument("--dry-run", action="store_true",
                    help="decode and log, but never write the database")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    log(f"config: {cfg['_path']}  site: {cfg['site']['name']}")

    guards = cfg["guards"]
    max_plausible = guards["max_plausible_watts"]
    min_interval = guards["min_interval_s"]
    rebase_after = guards["rebase_after_s"]
    coherence_k = guards["coherence_k"]
    unmetered_max = cfg["unmetered"]["max_watts"]
    mains_units = cfgmod.mains_units(cfg)

    sources = build_sources(cfg, args)
    if not sources:
        cfgmod.die("no sources: define meters in the config, or use --tcp/--gateway")

    feed = LiveFeed(cfg["collector"].get("live_feed_port", 0),
                    cfg["collector"].get("live_feed_bind", "127.0.0.1"))
    store = SqliteStore(cfg["database"]["path"],
                        cfg["collector"]["flush_interval"], args.dry_run)

    prev, prev_wall = {}, {}
    counts = {"ok": 0, "bad": 0, "rebase": 0, "glitch": 0}

    q = queue.Queue(maxsize=SPOOL_CAP)
    for label, unit, src in sources:
        bound = f"unit {unit}" if unit is not None else "unit from packet"
        log(f"source {label} -> {bound}")
        threading.Thread(target=pump, args=(label, unit, src, q, counts),
                         daemon=True).start()

    while True:
        unit_override, packet = q.get()
        d = parse_payload(packet[3:])
        # The port wins over the packet: every ECM-1240 ships unit_id=0.
        unit = unit_override if unit_override is not None else d["unit_id"]
        cal = cfgmod.calibration(cfg, unit)
        counts["ok"] += 1
        now_wall = time.time()

        p = prev.get(unit)
        if p is None:                             # first packet seen for a unit
            prev[unit], prev_wall[unit] = d, now_wall
            log(f"unit {unit}: baseline packet, volts={d['volts']:.1f}")
            continue
        if d["flag"] & 0x01:                      # meter declared a counter reset
            counts["rebase"] += 1
            prev[unit], prev_wall[unit] = d, now_wall
            log(f"unit {unit}: counter reset (flag=0x{d['flag']:02x}) — re-baselined")
            continue

        dsecs = d["secs"] - p["secs"]
        if dsecs <= 0:
            dsecs += 1 << 24                      # seconds counter wrap
        dwall = now_wall - prev_wall[unit]
        if dsecs > rebase_after:                  # long gap (or a frozen counter that
            counts["rebase"] += 1                 # wrapped to something huge): resync
            prev[unit], prev_wall[unit] = d, now_wall
            log(f"unit {unit}: {dsecs}s gap — re-baselined")
            continue
        w = watts(d, p)

        # Consistency guards. On a bad interval the VALUE is discarded but the
        # last-good packet is HELD as prev, so the next good packet's delta
        # spans the gap. The watt-second counters are cumulative, so average
        # watts self-heal across a skipped packet with no loss of energy
        # accuracy; a sustained fault just leaves an honest gap.
        #
        #  - coherence: real watts can never exceed apparent power (V*A).
        #    ch1_amps is the meter's own current, straight from the packet.
        #    Holds for any ch1, mains or branch.
        #  - branchsum: the mains cannot exceed the sum of the metered branches
        #    by more than the plausible unmetered load. Independent of ch1's
        #    own amps, which can inflate along with a faulty counter. Mains
        #    units only — where ch1 is an ordinary branch the rule is void.
        #  - burst: a sub-cadence seconds delta makes watts() unreliable.
        ch1_va = d["volts"] * d["ch1_amps"]
        mains = w["ch1"] * cal.get("ch1", 1.0)
        branch_sum = w["ch2"] + sum(w[f"aux{i}"] for i in range(1, 6))
        reason = detail = limit_w = None
        if not all(abs(v) <= max_plausible for v in w.values()):
            reason, detail = "implausible", "implausible"
        elif dsecs < min_interval:
            reason, detail = "burst", f"burst dsecs={dsecs}"
        elif w["ch1"] > ch1_va * coherence_k + 150:
            reason = "coherence"
            detail = f"ch1 {w['ch1']:.0f}W > {ch1_va:.0f}VA ceiling"
            limit_w = round(ch1_va * coherence_k + 150, 1)
        elif unit in mains_units and mains > branch_sum + unmetered_max:
            reason = "branchsum"
            detail = (f"mains {mains:.0f}W > branches {branch_sum:.0f}W "
                      f"+{unmetered_max}")
            limit_w = round(branch_sum + unmetered_max, 1)

        if reason is not None:
            counts["glitch"] += 1
            store.add_discard({"ts": round(now_wall), "unit_id": unit,
                               "reason": reason, "ch1_w": round(mains, 1),
                               "limit_w": limit_w})
            # Preserve the whole packet (calibrated) for later recovery. A
            # branch-sum rejection is coherent by construction — it passed the
            # V*A check above — so it is a real load that can be promoted back.
            try:
                qrec = {"ts": round(now_wall), "unit_id": unit, "reason": reason,
                        "volts": round(d["volts"], 1), "limit_w": limit_w}
                qrec.update({ch: round(w[ch] * cal.get(ch, 1.0), 1)
                             for ch in CHANNELS})
                store.add_quarantine(qrec)
            except Exception:
                pass

            # A MAINS-ONLY fault means ch1 is the liar but the BRANCH channels
            # are sound — the branch sum is literally how the mains was caught
            # lying. Salvage them: store the reading with ch1 = NULL rather
            # than throwing the whole packet away, so a failing mains sensor
            # blanks one number instead of freezing the entire meter.
            # burst/implausible mean the whole packet is unreliable -> drop it.
            mains_only = reason == "branchsum" or (
                reason == "coherence" and unit in mains_units)
            if not mains_only:
                log(f"unit {unit}: {detail} — discarded, holding prev; "
                    f"{raw_dump(d, dsecs, dwall, w)}")
                continue
            prev[unit], prev_wall[unit] = d, now_wall
            for ch in CHANNELS:
                w[ch] *= cal.get(ch, 1.0)
            reading = {"ts": round(time.time()), "unit_id": unit,
                       "volts": round(d["volts"], 1), "dsecs": dsecs}
            reading.update({ch: round(w[ch], 1) for ch in CHANNELS})
            reading["ch1"] = None                 # mains untrustworthy this instant
            store.add(reading)                    # live feed NOT broadcast -> holds last
            log(f"unit {unit}: {detail} — mains BLANKED, branches kept "
                f"(branches={branch_sum:.0f}W); {raw_dump(d, dsecs, dwall, w)}")
            continue

        prev[unit], prev_wall[unit] = d, now_wall
        for ch in CHANNELS:
            w[ch] *= cal.get(ch, 1.0)
        vals = [w["ch1"], w["ch2"]] + [w[f"aux{i}"] for i in range(1, 6)]
        feed.broadcast("E," + ",".join(
            [str(unit), str(round(d["volts"] * 10))] + [str(round(v)) for v in vals]))
        reading = {"ts": round(time.time()), "unit_id": unit,
                   "volts": round(d["volts"], 1), "dsecs": dsecs}
        reading.update({ch: round(w[ch], 1) for ch in CHANNELS})
        store.add(reading)
        log(f"unit {unit}: {d['volts']:.1f}V  " +
            " ".join(f"{ch}={w[ch]:.0f}W" for ch in CHANNELS))


if __name__ == "__main__":
    main()
