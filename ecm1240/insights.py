#!/usr/bin/env python3
"""Appliance-health rules — turns a watt series into plain-language findings.

Run as a batch job (a systemd timer every 15 minutes is typical), writing
`insights.json` next to the database. The API serves that file. A full pass
takes tens of seconds on a Raspberry Pi, so computing it per request would
block the API for most of a minute.

    python3 -m ecm1240.insights [--config config.yaml]

WHAT IT NEEDS
-------------
A `profiles.yaml` describing your circuits. There are no built-in profiles:
the thresholds that separate "running" from "off" depend entirely on YOUR
appliances, and someone else's numbers would produce confident nonsense about
your home. Generate a starting point from your own recorded data:

    python3 tools/suggest_profiles.py > profiles.yaml

Without that file the pass runs and reports nothing, which is the honest
outcome — silence is better than invented verdicts.

PROFILE KINDS
-------------
  cycling     compressor-style; expected to switch on and off all day
  multistate  variable-speed; several legitimate power plateaus
  burst       human-driven; runs when someone uses it, silence is normal
  always_on   expected to hold a steady floor; the floor is what is watched
"""

import argparse
import json
import os
import statistics
import sqlite3
import sys
import time

from . import config as cfgmod
from .protocol import CHANNELS

try:
    import yaml
except ImportError:
    yaml = None

OK, WATCH, ALERT, LEARNING, INFO = "ok", "watch", "alert", "learning", "info"
LEVEL_RANK = {ALERT: 0, WATCH: 1, LEARNING: 2, OK: 3, INFO: 4}

MAX_GAP_S = 120            # a longer hole in the data breaks a run in two
BASELINE_DAYS = 14         # trailing window the drift rules want
MIN_DRIFT_DAYS = 10        # ...and the minimum before they will call a verdict
CYCLE_WINDOW_H = 24        # look-back for the cycle rules
HIST_KEEP_DAYS = 120


# ── data access ──────────────────────────────────────────────────────────────

def connect(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA query_only=ON")
    return db


def load_window(db, unit, since):
    """All channels for one meter since `since`, as ts[] + volts[] + {ch: watts[]}.

    One query per meter rather than one per channel: on a small Pi the
    per-query overhead dominates, and every rule for a meter runs off this
    single pass.
    """
    cols = ",".join(CHANNELS)
    rows = db.execute(
        f"SELECT ts,volts,{cols} FROM readings WHERE unit_id=? AND ts>=? ORDER BY ts",
        (unit, since)).fetchall()
    ts = [r[0] for r in rows]
    volts = [r[1] for r in rows]
    series = {ch: [r[2 + i] for r in rows] for i, ch in enumerate(CHANNELS)}
    return ts, volts, series


def history_days(db, unit):
    row = db.execute("SELECT MIN(ts), MAX(ts) FROM readings WHERE unit_id=?",
                     (unit,)).fetchone()
    if not row or row[0] is None:
        return 0.0
    return (row[1] - row[0]) / 86400.0


# ── segmentation ─────────────────────────────────────────────────────────────

def segment(ts, watts, on_thr, off_thr, min_dur, floor=0.0):
    """Split a series into on/off runs: gap-aware, de-blipped, floor-relative.

    Returns [{"on", "start", "end", "dur", "med", "peak"}].

    Hysteresis (on_thr > off_thr) stops a load hovering near the threshold from
    producing a hundred phantom cycles. `min_dur` then absorbs any surviving
    blip into its neighbour, which is what separates a real four-minute
    compressor run from one-second noise.

    `floor` is the circuit's standby draw. The on/off decision is made on watts
    ABOVE that floor, so a rising standby cannot pin the rule on — but med and
    peak stay ABSOLUTE, because those numbers get shown to a person and have to
    match what the history graph shows.
    """
    raw, state, start, vals = [], None, None, []
    gapped = False
    prev_t = None
    for t, w in zip(ts, watts):
        if w is None:
            continue
        lw = w - floor
        if state is None:
            state, start, vals, prev_t = lw > on_thr, t, [w], t
            continue
        # A gap means we do not know what happened in between. Close the run at
        # the last sample actually seen and restart after the gap.
        if t - prev_t > MAX_GAP_S:
            raw.append([state, start, prev_t, vals, gapped])
            state, start, vals, gapped, prev_t = lw > on_thr, t, [w], True, t
            continue
        nxt = True if lw > on_thr else (False if lw < off_thr else state)
        if nxt != state:
            raw.append([state, start, prev_t, vals, gapped])
            state, start, vals, gapped = nxt, prev_t, [w], False
        else:
            vals.append(w)
        prev_t = t
    if state is not None:
        raw.append([state, start, ts[-1] if ts else start, vals, gapped])

    # Absorb sub-min_dur segments into whichever neighbour they interrupt.
    merged = []
    for seg in raw:
        dur = seg[2] - seg[1]
        if seg[4]:
            # Begins after a data gap. NEVER merge it backwards: we do not know
            # what the circuit did while the collector was down, and gluing the
            # two sides together silently undoes the gap break above — turning
            # two ordinary runs either side of an outage into one phantom
            # marathon run that then trips the stuck-on rule.
            merged.append(seg)
        elif merged and dur < min_dur and merged[-1][0] != seg[0]:
            merged[-1][2] = seg[2]
            merged[-1][3].extend(seg[3])
        elif merged and merged[-1][0] == seg[0]:
            merged[-1][2] = seg[2]
            merged[-1][3].extend(seg[3])
        else:
            merged.append(seg)

    out = []
    for on, s, e, vals, _ in merged:
        vals = [v for v in vals if v is not None]
        out.append(dict(on=on, start=s, end=e, dur=e - s,
                        med=statistics.median(vals) if vals else 0.0,
                        peak=max(vals) if vals else 0.0))
    return out


def circuit_floors(db, unit, days=BASELINE_DAYS):
    """Every channel's standby floor: the median of its recent daily minimums.

    Daily MIN rather than a percentile of the live window, because a circuit
    that really IS stuck on would drag a live-window percentile up to its own
    running wattage — the floor would swallow the fault and the stuck-on rule
    would fall silent exactly when it matters. A median across two weeks of
    daily minimums cannot be moved by one bad day.

    Today is excluded for the same reason: it is a partial day and, if
    something is stuck right now, a dishonest one.
    """
    floors = {}
    for ch in CHANNELS:
        rows = db.execute(
            f"SELECT date(ts,'unixepoch','localtime') d, MIN({ch}) m"
            " FROM readings WHERE unit_id=? AND ts>=? AND {c} IS NOT NULL"
            " GROUP BY d ORDER BY d".replace("{c}", ch),
            (unit, int(time.time()) - days * 86400)).fetchall()
        mins = [r[1] for r in rows[:-1] if r[1] is not None]
        floors[ch] = statistics.median(mins) if mins else 0.0
    return floors


# ── presentation helpers ─────────────────────────────────────────────────────

def dur_txt(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{sec // 60} min"
    h, m = divmod(sec // 60, 60)
    return f"{h} h {m:02d} m" if m else f"{h} h"


def w_txt(w):
    return f"{w / 1000:.1f} kW" if abs(w) >= 1000 else f"{w:.0f} W"


def worse_of(a, b):
    if a is None:
        return b
    return a if LEVEL_RANK[a] <= LEVEL_RANK[b] else b


def card(cid, prof, level, title, detail, stat=None, **extra):
    out = {"id": f"{prof['unit']}:{prof['ch']}:{cid}",
           "circuit": prof["name"], "unit": prof["unit"], "ch": prof["ch"],
           "level": level, "title": title, "detail": detail}
    if stat:
        out["stat"] = stat
    out.update(extra)
    return out


# ── rules ────────────────────────────────────────────────────────────────────

def rules_cycling(prof, segs, floor):
    """Compressor-style: short-cycling, start rate, stuck on, duty."""
    name = prof["name"]
    runs = [s for s in segs if s["on"]]
    cards = []
    if not runs:
        return [card("idle", prof, OK, f"{name} has not run",
                     f"No run above {w_txt(prof['on'])} in the last "
                     f"{CYCLE_WINDOW_H} h.")]

    short_s = prof.get("short_run_s")
    if short_s:
        shorts = [s for s in runs if s["dur"] < short_s]
        if len(shorts) >= 3:
            cards.append(card(
                "short_cycle", prof, ALERT, f"{name} is short-cycling",
                f"{len(shorts)} runs shorter than {dur_txt(short_s)} in the last "
                f"{CYCLE_WINDOW_H} h. Repeated short runs are hard on a "
                "compressor and usually mean it is switching off before it has "
                "done any useful work.",
                stat=f"{len(shorts)} short runs"))

    per_h = prof.get("starts_per_h")
    if per_h:
        window_h = max((segs[-1]["end"] - segs[0]["start"]) / 3600.0, 1.0)
        rate = len(runs) / window_h
        if rate > per_h:
            cards.append(card(
                "start_rate", prof, WATCH, f"{name} is starting often",
                f"{rate:.1f} starts an hour, against an expected {per_h}.",
                stat=f"{rate:.1f}/h"))

    long_h = prof.get("long_run_h")
    if long_h:
        longest = max(runs, key=lambda s: s["dur"])
        if longest["dur"] > long_h * 3600:
            still_on = longest is runs[-1] and not segs[-1]["on"] is False
            level = ALERT if still_on else WATCH
            cid = "stuck_on" if still_on else "long_run"
            verb = "has been running non-stop" if still_on else "ran for a long stretch"
            hint = prof.get("stuck_hint" if still_on else "long_hint", "")
            cards.append(card(
                cid, prof, level, f"{name} {verb}",
                f"{dur_txt(longest['dur'])} continuously, drawing about "
                f"{w_txt(longest['med'])}. " + hint,
                stat=dur_txt(longest["dur"])))

    if prof.get("duty"):
        span = max(segs[-1]["end"] - segs[0]["start"], 1)
        on_s = sum(s["dur"] for s in runs)
        duty = 100.0 * on_s / span
        high = prof.get("duty_high", 65)
        if duty > high:
            cards.append(card(
                "duty_high", prof, WATCH, f"{name} runs most of the day",
                f"On for {duty:.0f}% of the last {CYCLE_WINDOW_H} h. Sustained "
                "high duty can mean it is struggling to keep up.",
                stat=f"{duty:.0f}% duty"))

    if not cards:
        cards.append(card(
            "cycling_ok", prof, OK, f"{name} is cycling normally",
            f"{len(runs)} runs in the last {CYCLE_WINDOW_H} h, typical run "
            f"{dur_txt(statistics.median([s['dur'] for s in runs]))} at "
            f"{w_txt(statistics.median([s['med'] for s in runs]))}.",
            stat=f"{len(runs)} runs"))
    return cards


def rules_multistate(prof, segs, floor):
    """Variable-speed: report the plateaus found, and flag never switching off."""
    name = prof["name"]
    runs = [s for s in segs if s["on"]]
    if not runs:
        return [card("idle", prof, OK, f"{name} has not run",
                     f"No activity above {w_txt(prof['on'])} in this window.")]
    long_h = prof.get("long_run_h")
    if long_h and runs[-1]["dur"] > long_h * 3600 and segs[-1]["on"]:
        return [card("stuck_on", prof, ALERT, f"{name} has not shut off",
                     f"Running continuously for {dur_txt(runs[-1]['dur'])} at "
                     f"about {w_txt(runs[-1]['med'])}.",
                     stat=dur_txt(runs[-1]["dur"]))]
    levels = sorted({round(s["med"] / 50) * 50 for s in runs})
    tot = sum(s["dur"] for s in runs)
    return [card("power_states", prof, OK, f"{name} looks normal",
                 f"Ran {dur_txt(tot)} across {len(runs)} periods, at "
                 f"{', '.join(w_txt(x) for x in levels)}.",
                 stat=dur_txt(tot))]


def rules_burst(prof, segs, floor):
    """Human-driven: silence is normal, so only a stuck-on state is a fault."""
    name = prof["name"]
    runs = [s for s in segs if s["on"]]
    if not runs:
        return [card("idle", prof, OK, f"{name} — no use",
                     f"Nothing above {w_txt(prof['on'])} in this window. For a "
                     "circuit used by hand, that is normal.")]
    long_h = prof.get("long_run_h", 6.0)
    if segs[-1]["on"] and runs[-1]["dur"] > long_h * 3600:
        return [card("stuck_on", prof, WATCH, f"{name} has been on a long time",
                     f"{dur_txt(runs[-1]['dur'])} continuously at about "
                     f"{w_txt(runs[-1]['med'])}. Was something left on?",
                     stat=dur_txt(runs[-1]["dur"]))]
    tot = sum(s["dur"] for s in runs)
    return [card("runtime", prof, OK, f"{name} used {dur_txt(tot)}",
                 f"{len(runs)} periods of use, peaking at "
                 f"{w_txt(max(s['peak'] for s in runs))}.",
                 stat=dur_txt(tot))]


def rules_always_on(prof, series, floor, days_of_history):
    """Steady-floor circuit: watch the floor itself, not the cycles."""
    name = prof["name"]
    vals = [w for w in series if w is not None]
    if not vals:
        return [card("nodata", prof, INFO, f"{name} — no data", "No readings.")]
    cur = statistics.median(vals[-720:] if len(vals) > 720 else vals)
    expect = prof.get("floor_w")
    if expect:
        if cur < expect * 0.5:
            return [card("floor_drop", prof, ALERT, f"{name} has gone quiet",
                         f"Drawing {w_txt(cur)}, against an expected "
                         f"{w_txt(expect)}. Something on this circuit may have "
                         "shut down or lost power.", stat=w_txt(cur))]
        if cur > expect * 1.5:
            return [card("floor_rise", prof, WATCH, f"{name} is drawing more",
                         f"Now {w_txt(cur)}, against an expected "
                         f"{w_txt(expect)}. Something new may have been added.",
                         stat=w_txt(cur))]
    if days_of_history < MIN_DRIFT_DAYS:
        return [card("learning", prof, LEARNING, f"{name} — still learning",
                     f"Holding steady at {w_txt(cur)}. Needs "
                     f"{MIN_DRIFT_DAYS:.0f} days of history before drift can be "
                     f"judged; {days_of_history:.1f} so far.", stat=w_txt(cur))]
    return [card("floor_ok", prof, OK, f"{name} is steady",
                 f"Holding at {w_txt(cur)}.", stat=w_txt(cur))]


def rule_voltage(cfg, volts_by_unit, ref_unit):
    """Service voltage excursions, judged on one meter.

    Judged on the meter carrying the mains, not on an average of them all: an
    ECM computes watts from its OWN voltage reference, so the whole-house figure
    is scaled by that meter's volts and by nothing else. Alarming on the same
    number the house total is built from is the point; an average would report a
    voltage no channel actually uses.

    Readings below `voltage.dead_below` are excluded from the verdict entirely.
    They are the meter losing its own supply rather than the service sagging —
    see the note on that setting in config.py. They get their own quiet note,
    because a meter that keeps blacking out is worth knowing about; it just is
    not a voltage fault, and it must not drag the reported range down to zero.
    """
    vcfg = cfg.get("voltage", {})
    lo_lim = float(vcfg.get("low", 114.0))
    hi_lim = float(vcfg.get("high", 126.0))
    dead_below = float(vcfg.get("dead_below", 90.0))
    prof = {"unit": ref_unit, "ch": "volts", "name": "Service voltage"}

    raw = [v for v in volts_by_unit.get(ref_unit, []) if v is not None]
    if not raw:
        return []
    live = [v for v in raw if v >= dead_below]
    dead = len(raw) - len(live)
    if not live:
        return [card("voltage_dead", prof, INFO, "No usable voltage readings",
                     f"Every reading in the last {CYCLE_WINDOW_H} h was below "
                     f"{dead_below:.0f} V, which means the meter has lost its "
                     "own power rather than the service being low — check its "
                     "AC adapter.", stat="meter unpowered")]

    lo, hi = min(live), max(live)
    n_low = sum(1 for v in live if v < lo_lim)
    n_high = sum(1 for v in live if v > hi_lim)
    cards = []
    if n_low or n_high:
        share = (n_low + n_high) / len(live) * 100
        # Say WHICH way it went: a sag and a surge are different faults with
        # different causes, and one message covering both explains the wrong one
        # to whoever reads it.
        if n_low and n_high:
            what = (f"{n_low:,} readings sagged below {lo_lim:.0f} V and "
                    f"{n_high:,} rose above {hi_lim:.0f} V")
            why = ("Sags under heavy load and a supply running hot are separate "
                   "problems; seeing both in a day is worth raising with your "
                   "utility.")
        elif n_low:
            what = (f"{n_low:,} reading{'s' if n_low != 1 else ''} sagged below "
                    f"{lo_lim:.0f} V, as low as {lo:.1f} V")
            why = ("A brief dip when something large starts is normal. A sag "
                   "that keeps happening starves motors and is worth raising "
                   "with your utility.")
        else:
            what = (f"{n_high:,} reading{'s' if n_high != 1 else ''} rose above "
                    f"{hi_lim:.0f} V, peaking at {hi:.1f} V")
            why = ("A supply running high shortens the life of everything "
                   "plugged into it — worth raising with your utility if it "
                   "keeps up.")
        cards.append(card("voltage", prof, WATCH if share < 1 else ALERT,
                          "Line voltage went out of range",
                          f"{what} in the last {CYCLE_WINDOW_H} h — {share:.1f}% "
                          f"of {len(live):,} readings, against the normal "
                          f"{lo_lim:.0f}–{hi_lim:.0f} V band. {why}",
                          stat=f"{lo:.0f}–{hi:.0f} V"))
    else:
        cards.append(card("voltage", prof, OK, "Line voltage is healthy",
                          f"Stayed between {lo:.1f} and {hi:.1f} V over the last "
                          f"{CYCLE_WINDOW_H} h, inside the "
                          f"{lo_lim:.0f}–{hi_lim:.0f} V band.",
                          stat=f"{lo:.0f}–{hi:.0f} V"))

    if dead:
        cards.append(card("voltage_dead", prof, INFO,
                          "The meter briefly lost power",
                          f"{dead:,} reading{'s' if dead != 1 else ''} in the "
                          f"last {CYCLE_WINDOW_H} h came in under "
                          f"{dead_below:.0f} V. That is not a brownout: the "
                          "meter senses the line through its own AC adapter, so "
                          "a power cut, an unplugged adapter or a lost sensing "
                          "lead all read as the voltage collapsing. Left out of "
                          "the verdict above. If it repeats while the lights "
                          "stay on, check the adapter and its socket.",
                          stat=f"{dead:,} dead"))
    return cards


def pair_index(ts_a, ts_b, max_skew_s=4):
    """Nearest-in-time (i, j) index pairs between two meters' samples.

    Two ECMs free-run on their own clocks, so they share almost no timestamps: a
    SQL join on a.ts = b.ts matches only the occasional coincidence, and a
    tolerant join needs a correlated subquery per row, which on a small Pi costs
    more than the whole pass. A two-pointer merge over windows already in memory
    costs nothing. Pairs further apart than max_skew_s are dropped — that is a
    gap on one side, not a pair.
    """
    out = []
    if not ts_a or not ts_b:
        return out
    j = 0
    for i, t in enumerate(ts_a):
        while j + 1 < len(ts_b) and abs(ts_b[j + 1] - t) <= abs(ts_b[j] - t):
            j += 1
        if abs(ts_b[j] - t) <= max_skew_s:
            out.append((i, j))
    return out


def pct(vals, q):
    """Order-statistic percentile (q in 0..1) of an unsorted list."""
    s = sorted(vals)
    return s[min(int(q * (len(s) - 1)), len(s) - 1)]


def unmetered_series(cfg, window):
    """Every paired instant as the mains minus the sum of the metered branches.

    `window` is {unit: (ts, series)}. With one meter this is a straight
    subtraction; with two it needs the pairing above, because their samples do
    not line up.
    """
    mains = cfgmod.mains_channel(cfg)
    if not mains:
        return []
    m_unit, m_ch = mains
    if m_unit not in window:
        return []
    branches = {}
    for c in cfgmod.channel_list(cfg):
        # Hidden means "not shown", not "not measured" — leaving a hidden
        # channel out would report its circuit as unmetered load.
        if c["role"] != "mains":
            branches.setdefault(c["unit"], []).append(c["channel"])

    ts_m, ser_m = window[m_unit]
    others = [u for u in branches if u != m_unit and u in window]
    pairs = {u: dict(pair_index(ts_m, window[u][0])) for u in others}

    out = []
    for i, t in enumerate(ts_m):
        house = ser_m[m_ch][i]
        if house is None:
            continue
        total = 0.0
        ok = True
        for ch in branches.get(m_unit, []):
            v = ser_m[ch][i]
            if v is None:
                ok = False
                break
            total += v
        for u in others:
            j = pairs[u].get(i)
            if j is None:                 # no sample close enough in time
                ok = False
                break
            for ch in branches[u]:
                v = window[u][1][ch][j]
                if v is None:
                    ok = False
                    break
                total += v
            if not ok:
                break
        if ok:
            out.append(house - total)
    return out


def rule_unmetered(cfg, window, min_samples=720):
    """How much of the house has no CT on it — and whether that is changing.

    The check a per-circuit monitor cannot do for itself: the mains sees every
    watt, the branch CTs see only what they are clamped around, and the gap
    between them is everything nobody is measuring. A gap that grows and stays
    is either something new plugged into an uncounted circuit, or a CT that has
    come loose and is quietly under-reading its own circuit.

    Judged on the MEDIAN and the 10th percentile rather than the peak or the
    mean, which is what makes it usable in a real house. Something large but
    occasional — an EV charger with no CT, a well pump, a workshop — is supposed
    to open the gap for a few hours; a load running four hours in twenty-four
    cannot move either statistic. A CT that has come off can never come back
    down, and shows up immediately.
    """
    prof = {"unit": 0, "ch": "__unmetered",
            "name": cfg.get("unmetered", {}).get("label", "Unmetered")}
    ucfg = cfg.get("unmetered", {})
    noise_lo = float(ucfg.get("noise_low_watts", -50))
    noise_hi = float(ucfg.get("noise_high_watts", 100))

    if not cfgmod.mains_channel(cfg):
        return []                 # no whole-house channel: nothing to compare
    vals = unmetered_series(cfg, window)
    if len(vals) < min_samples:
        return [card("unmetered", prof, INFO,
                     f"{prof['name']} — not enough paired readings",
                     f"{len(vals):,} moments in the last {CYCLE_WINDOW_H} h had "
                     "every meter reporting together. Normal shortly after a "
                     "restart, or if a meter has been offline.")]

    floor, typ = pct(vals, 0.10), pct(vals, 0.50)
    high = sum(1 for w in vals if w > noise_hi) / len(vals) * 100
    stat = f"{typ:+.0f} W typical"

    if floor > noise_hi:
        return [card("unmetered", prof, ALERT,
                     "Something is drawing power that no CT sees",
                     f"The mains reads {w_txt(floor)} more than every metered "
                     "circuit added up even at its quietest, and "
                     f"{w_txt(typ)} more typically ({high:.0f}% of the last "
                     f"{CYCLE_WINDOW_H} h above {w_txt(noise_hi)}). A gap that "
                     "never closes is either a circuit with no CT on it, or a "
                     "CT that has come loose and is under-reading. That floor "
                     f"alone is {floor * 24 / 1000:.1f} kWh a day.",
                     stat=f"{floor:+.0f} W floor")]
    if typ > noise_hi:
        return [card("unmetered", prof, WATCH,
                     "More is being used than the CTs account for",
                     f"For most of the last {CYCLE_WINDOW_H} h the mains read "
                     f"about {w_txt(typ)} more than the metered circuits summed "
                     f"to ({high:.0f}% of readings above {w_txt(noise_hi)}), "
                     f"falling back to {w_txt(floor)} at its quietest. If it "
                     "stays up, look for a CT that has come off its conductor "
                     "or a circuit nobody has clamped.",
                     stat=stat)]
    if typ < noise_lo:
        return [card("unmetered", prof, WATCH,
                     "The CTs add up to more than the mains",
                     f"Typically {w_txt(-typ)} more, which is backwards — every "
                     "watt has to cross the mains CT to reach a branch. That "
                     "points at two CTs on the same circuit, a branch CT set to "
                     "the wrong range, or a mains channel reading low. "
                     "CALIBRATION.md covers checking a channel against a clamp "
                     "meter.", stat=stat)]
    return [card("unmetered", prof, OK, "Every watt is accounted for",
                 f"The mains and the metered circuits agree to within "
                 f"{w_txt(abs(typ))} for most of the day — inside the "
                 f"{noise_lo:.0f} to {noise_hi:.0f} W the CTs' own tolerance "
                 "explains.", stat=stat)]


DISPATCH = {"cycling": rules_cycling, "multistate": rules_multistate,
            "burst": rules_burst}


# ── profiles ─────────────────────────────────────────────────────────────────

def load_profiles(cfg):
    path = cfg.get("insights", {}).get("profiles", "profiles.yaml")
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(cfg.get("_path", ".")))
        path = os.path.join(base, path)
    if not os.path.exists(path):
        return [], path
    if yaml is None:
        return [], path
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    profs = data.get("profiles") or []
    for p in profs:
        p.setdefault("kind", "burst")
        p.setdefault("min_dur", 120)
        p.setdefault("on", 100)
        p.setdefault("off", max(1, p["on"] * 0.6))
        if "name" not in p:
            p["name"] = cfgmod.channel_label(cfg, p.get("unit", 0), p.get("ch", "ch1"))
    return profs, path


# ── main pass ────────────────────────────────────────────────────────────────

def run(cfg):
    db_path = cfg["database"]["path"]
    if not os.path.exists(db_path):
        return {"generated_at": int(time.time()), "cards": [],
                "error": f"no database at {db_path}"}
    profs, prof_path = load_profiles(cfg)
    db = connect(db_path)
    now_ts = int(time.time())
    since = now_ts - CYCLE_WINDOW_H * 3600
    cards = []

    # Whole-house checks first. These need no profiles at all — they run off the
    # config and the readings — so a fresh installation gets a working voltage
    # check and an honest unmetered figure on day one, before anyone has sat
    # down to describe their appliances.
    window, volts_by_unit = {}, {}
    for u in sorted({c["unit"] for c in cfgmod.channel_list(cfg)}):
        ts_u, volts_u, series_u = load_window(db, u, since)
        if ts_u:
            window[u] = (ts_u, series_u)
            volts_by_unit[u] = volts_u
    mains = cfgmod.mains_channel(cfg)
    ref_unit = mains[0] if mains else (min(volts_by_unit) if volts_by_unit else 0)
    cards += rule_voltage(cfg, volts_by_unit, ref_unit)
    cards += rule_unmetered(cfg, window)

    if not profs:
        cards.append({
            "id": "setup", "circuit": "Setup", "level": INFO,
            "title": "No appliance profiles configured",
            "detail": (
                "Insights need to know which circuit is which appliance and what "
                "counts as 'running' for each one. Those numbers depend on your "
                "own appliances, so there are no defaults. Generate a starting "
                "point from your recorded data:\n\n"
                "    python3 tools/suggest_profiles.py > profiles.yaml\n\n"
                f"Expected at: {prof_path}")})
        cards.sort(key=lambda c: LEVEL_RANK.get(c["level"], 9))
        return {"generated_at": now_ts, "cards": cards, "profiles": 0,
                "window_h": CYCLE_WINDOW_H}

    by_unit = {}
    for p in profs:
        by_unit.setdefault(p.get("unit", 0), []).append(p)

    for unit, unit_profs in sorted(by_unit.items()):
        # Reuse the window already read for the whole-house checks; only a meter
        # with profiles but no configured channels needs its own query.
        if unit in window:
            ts, series = window[unit]
        else:
            ts, _volts, series = load_window(db, unit, since)
        if not ts:
            continue
        floors = circuit_floors(db, unit)
        days = history_days(db, unit)
        for prof in unit_profs:
            ch = prof.get("ch")
            if ch not in CHANNELS:
                continue
            prof = dict(prof, unit=unit, ch=ch)
            floor = floors.get(ch, 0.0)
            kind = prof.get("kind", "burst")
            try:
                if kind == "always_on":
                    cards += rules_always_on(prof, series[ch], floor, days)
                elif kind == "excluded":
                    continue
                else:
                    segs = segment(ts, series[ch], prof["on"], prof["off"],
                                   prof["min_dur"], floor)
                    if segs:
                        cards += DISPATCH.get(kind, rules_burst)(prof, segs, floor)
            except (ValueError, KeyError, statistics.StatisticsError) as e:
                cards.append(card("error", prof, INFO,
                                  f"{prof['name']} — rule error", str(e)))

    cards.sort(key=lambda c: LEVEL_RANK.get(c["level"], 9))
    return {"generated_at": now_ts, "cards": cards, "profiles": len(profs),
            "window_h": CYCLE_WINDOW_H}


def append_history(path, result):
    """Day-by-day record of every finding, so each card can show its history."""
    try:
        with open(path) as fh:
            hist = json.load(fh)
    except (OSError, ValueError):
        hist = {"findings": {}}
    day = time.strftime("%Y-%m-%d", time.localtime(result["generated_at"]))
    for c in result["cards"]:
        if c["level"] == INFO:
            continue
        rec = hist["findings"].setdefault(c["id"], {"days": {}})
        rec["circuit"] = c.get("circuit")
        rec["title"] = c.get("title")
        d = rec["days"].setdefault(day, {})
        d["level"] = worse_of(d.get("level"), c["level"])
        d["detail"] = c.get("detail")
    cutoff = time.strftime("%Y-%m-%d",
                           time.localtime(time.time() - HIST_KEEP_DAYS * 86400))
    for rec in hist["findings"].values():
        for k in [d for d in rec["days"] if d < cutoff]:
            del rec["days"][k]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(hist, fh)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Compute appliance-health insights")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--stdout", action="store_true",
                    help="print the result instead of writing insights.json")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    result = run(cfg)

    if args.stdout:
        json.dump(result, sys.stdout, indent=2)
        print()
        return

    base = os.path.dirname(os.path.abspath(cfg["database"]["path"]))
    out = cfg.get("insights", {}).get("output", os.path.join(base, "insights.json"))
    hist = cfg.get("insights", {}).get("history",
                                       os.path.join(base, "insight-history.json"))
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh)
    os.replace(tmp, out)
    try:
        append_history(hist, result)
    except OSError:
        pass
    print(f"{len(result['cards'])} cards -> {out}")


if __name__ == "__main__":
    main()
