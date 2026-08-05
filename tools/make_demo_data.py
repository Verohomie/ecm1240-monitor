#!/usr/bin/env python3
"""Generate a plausible week of FAKE energy data.

Two reasons this exists:

  1. You can run the whole stack — collector schema, API, dashboard — and see
     what it looks like BEFORE buying a meter or opening your panel.
  2. Every screenshot in the documentation comes from this, not from a real
     house. Real energy data shows when a home is occupied and when it is
     empty; it should not be published. Neither should yours.

The data is synthesised from simple models (a cycling compressor, a daytime
pump, human-driven bursts), not sampled from anyone's home.

    python3 tools/make_demo_data.py --db demo.db --days 7
    python3 -m ecm1240.api --config config.demo.yaml

Then open http://127.0.0.1:8080/
"""

import argparse
import math
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ecm1240.collector import SqliteStore  # noqa: E402
from ecm1240.protocol import CHANNELS      # noqa: E402

STEP_S = 5          # sample cadence, matching a real meter's default


def cycling(t, period_s, duty, on_w, off_w, jitter=0.06):
    """Compressor-style load: a repeating on/off cycle."""
    phase = (t % period_s) / period_s
    w = on_w if phase < duty else off_w
    return max(0.0, w * (1 + random.uniform(-jitter, jitter)))


def daytime(t, start_h, end_h, on_w, off_w, tz_offset=0):
    """A load on a fixed daily timer, e.g. a pool pump."""
    hour = ((t + tz_offset) % 86400) / 3600.0
    return on_w if start_h <= hour < end_h else off_w


def human(t, seed, base_w, peak_w, busy_hours=(7, 9, 17, 22)):
    """Human-driven bursts: quiet at night, active morning and evening."""
    hour = (t % 86400) / 3600.0
    rng = random.Random(int(t // 300) ^ seed)
    active = (busy_hours[0] <= hour < busy_hours[1] or
              busy_hours[2] <= hour < busy_hours[3])
    if active and rng.random() < 0.35:
        return base_w + rng.random() * (peak_w - base_w)
    return base_w * rng.uniform(0.8, 1.2)


def always_on(t, floor_w, swing=0.05):
    drift = math.sin(t / 3600.0) * floor_w * swing
    return max(0.0, floor_w + drift + random.uniform(-2, 2))


def generate(db_path, days, meters):
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(SqliteStore.SCHEMA)
    db.execute(SqliteStore.SCHEMA_DISCARDS)
    db.execute(SqliteStore.SCHEMA_QUARANTINE)

    end = int(time.time())
    start = end - days * 86400
    random.seed(20260101)

    rows = []
    total = 0
    for ts in range(start, end, STEP_S):
        # Branch circuits on meter 0.
        b0 = {
            "ch2":  daytime(ts, 10, 16, 675, 25),          # pump on a timer
            "aux1": cycling(ts, 1800, 0.45, 2650, 60),     # compressor
            "aux2": human(ts, 2, 12, 240),                 # workshop
            "aux3": cycling(ts, 2400, 0.55, 155, 19),      # fridge-like
            "aux4": human(ts, 4, 8, 190, (6, 8, 18, 23)),  # lighting
            "aux5": always_on(ts, 160),                    # equipment rack
        }
        # Branch circuits on meter 1 (only when a second meter is simulated).
        b1 = {
            "ch1":  human(ts, 11, 4, 3200, (7, 9, 17, 20)),   # cooking
            "ch2":  cycling(ts, 9000, 0.12, 4300, 6),         # water heater
            "aux1": human(ts, 13, 3, 2400, (9, 12, 19, 21)),  # laundry
            "aux2": human(ts, 14, 30, 320),
            "aux3": human(ts, 15, 25, 180),
            "aux4": always_on(ts, 42),
            "aux5": human(ts, 17, 35, 260, (17, 19, 20, 23)),
        } if 1 in meters else {}

        # The mains channel covers the WHOLE service, so it must include every
        # branch on BOTH meters plus a small remainder for circuits with no CT.
        # Getting this wrong makes per-circuit shares add up to more than 100%.
        mains = sum(b0.values()) + sum(b1.values()) + random.uniform(60, 140)

        for unit in meters:
            vals = {"ch1": mains, **b0} if unit == 0 else b1
            # dsecs is what a real meter reports: how many seconds this row's
            # watts are the average over. The generator is on a perfect
            # metronome, so it is always STEP_S here — a real one wanders.
            row = {"ts": ts, "unit_id": unit, "dsecs": STEP_S,
                   "volts": round(random.uniform(119.2, 121.4), 1)}
            row.update({ch: round(vals.get(ch, 0.0), 1) for ch in CHANNELS})
            rows.append(row)
            total += 1

        if len(rows) >= 20000:
            _flush(db, rows)
            rows = []
    _flush(db, rows)
    db.commit()
    db.close()
    return total


def _flush(db, rows):
    if not rows:
        return
    db.executemany(
        "INSERT OR REPLACE INTO readings"
        " (ts,unit_id,volts,ch1,ch2,aux1,aux2,aux3,aux4,aux5,dsecs)"
        " VALUES (:ts,:unit_id,:volts,:ch1,:ch2,:aux1,:aux2,:aux3,:aux4,:aux5,"
        ":dsecs)", rows)


def main():
    ap = argparse.ArgumentParser(description="Generate fake ECM-1240 data for a demo")
    ap.add_argument("--db", default="demo.db", help="output database (default demo.db)")
    ap.add_argument("--days", type=int, default=7, help="days of history (default 7)")
    ap.add_argument("--meters", type=int, default=2, choices=(1, 2),
                    help="how many meters to simulate (default 2)")
    args = ap.parse_args()

    print(f"generating {args.days} days at {STEP_S}s intervals ...")
    n = generate(args.db, args.days, list(range(args.meters)))
    size = os.path.getsize(args.db) / 1e6
    print(f"wrote {n:,} readings to {args.db} ({size:.1f} MB)\n")
    print("Next:")
    print("  cp config.demo.yaml config.yaml    # if you have no config yet")
    print(f"  python3 -m ecm1240.api --config config.demo.yaml")
    print("  open http://127.0.0.1:8080/")


if __name__ == "__main__":
    main()
