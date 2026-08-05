#!/usr/bin/env python3
"""History bucket test — no hardware required.

/api/history averages readings into buckets. Since readings started recording
how many seconds each one covers (`dsecs`), that average is energy-weighted
rather than reading-weighted, and there are two ways that can go wrong. Both
are checked here against the real endpoint, driven through Flask's test client
over a throwaway database, so what is under test is the SQL the API actually
serves rather than a re-implementation of it.

  1. AN EXISTING DATABASE MUST READ THE SAME. The weighting arrived after the
     first release, so most people's stored history predates the column and
     holds NULL there. Those readings fall back to the nominal cadence — every
     weight equal, which is arithmetically the plain average they were served
     with before. This asserts it: record what every bucket reads with no
     column at all, apply the collector's migration, ask again, and require
     every bucket to come back the same. It also asserts the running process
     NOTICES the column appear, because a test that quietly stayed on the old
     path would pass without checking anything.

  2. ONE LONG READING MUST NOT DECIDE A BUCKET. The collector stores any gap up
     to `guards.rebase_after_s` (120 s by default) before it resyncs instead,
     so the first reading after a serial hiccup or a restart can legitimately
     cover a minute and a half. Weighted literally it outvotes eighteen
     ordinary readings and takes the bucket over, though the energy it stands
     for is spread across several buckets either side. SNAP_MAX_INTERVAL_S caps
     it. One long reading among ten ordinary ones, in a single bucket, must
     land on the capped answer.

    python3 tools/test_history_buckets.py

Exits 0 on success, 1 on failure. Run it after any change to /api/history.
Requires Flask; no meter, no serial port, no config file.
"""
import json
import os
import random
import sqlite3
import sys
import tempfile
import urllib.parse
from decimal import Decimal
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ecm1240 import api                                      # noqa: E402
from ecm1240.protocol import CHANNELS                        # noqa: E402

TMP = tempfile.mkdtemp(prefix="ecm-history-test-")
DB = os.path.join(TMP, "energy.db")
DEAD_BELOW = 90.0
api.create_app({"database": {"path": DB}, "voltage": {"dead_below": DEAD_BELOW}})

# The readings table minus `dsecs`, i.e. any database written by an earlier
# release. Kept literal rather than imported so this test needs no pyserial.
# Source: ecm1240/collector.py, Store.SCHEMA and Store.MIGRATIONS.
SCHEMA_PRE_DSECS = (
    "CREATE TABLE readings ("
    " ts INTEGER NOT NULL, unit_id INTEGER NOT NULL, volts REAL,"
    " ch1 REAL, ch2 REAL, aux1 REAL, aux2 REAL, aux3 REAL, aux4 REAL, aux5 REAL,"
    " PRIMARY KEY (ts, unit_id)) WITHOUT ROWID"
)
ADD_DSECS = "ALTER TABLE readings ADD COLUMN dsecs INTEGER"

COLS = ("ts", "unit_id", "volts", "ch1", "ch2", "aux1", "aux2", "aux3", "aux4", "aux5")
END = 1700000000        # fixed so a run always produces the same buckets


def fresh_db(with_dsecs=False):
    """A new empty store at DB, with or without the interval column."""
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.execute(SCHEMA_PRE_DSECS)
    if with_dsecs:
        con.execute(ADD_DSECS)
    con.commit()
    return con


def get(client, query):
    r = client.get("/api/history?" + query)
    assert r.status_code == 200, f"{query} -> HTTP {r.status_code}: {r.data[:200]}"
    return json.loads(r.data)


def is_rounding_tie(query, bucket, span, before_w, after_w):
    """Is this bucket's 0.1 W difference only a coin-toss at .05?

    The plain average and the weighted one are the same number, but they reach
    it by different sums, so their last binary digit can differ. That only ever
    shows on screen when the true answer sits EXACTLY on a rounding boundary: a
    bucket whose mean is 634.05 can honestly be drawn as 634.0 or as 634.1, and
    which one appears is decided by a crumb far below a watt.

    Rather than allow a blanket 0.1 W of slack, this recomputes the bucket
    exactly. Watts are stored to one decimal place, so the values are exact
    decimals and their mean is an exact fraction. The difference is forgiven
    only if that fraction lands precisely on the boundary AND the two answers
    are its two legal roundings. Anything else is the average really moving.
    """
    p = dict(urllib.parse.parse_qsl(query))
    col = (f"CASE WHEN volts >= {DEAD_BELOW} THEN volts END"
           if p["channel"] == "volts" else p["channel"])
    end = int(p["end"])
    since = end - int(p["minutes"]) * 60
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    vals = [Decimal(str(v)) for (v,) in con.execute(
        f"SELECT {col} FROM readings WHERE unit_id = ? AND ts >= ? AND ts <= ?"
        f" AND (ts/{span})*{span} = ?", (int(p["unit"]), since, end, bucket))
        if v is not None]
    con.close()
    if not vals:
        return False
    exact = (Fraction(max(vals)) if p.get("agg") == "max"
             else Fraction(sum(vals)) / len(vals))
    tenths = exact * 10
    if tenths % 1 != Fraction(1, 2):            # not on a boundary -> a real move
        return False
    legal = {float((tenths - Fraction(1, 2)) / 10),
             float((tenths + Fraction(1, 2)) / 10)}
    return {before_w, after_w} == legal


# ── 1. an existing database must read the same ──────────────────────────────
QUERIES = [
    "unit=0&channel=ch1&minutes=120&points=120&end=%d" % END,   # 60 s buckets
    "unit=0&channel=ch1&minutes=120&points=37&end=%d" % END,    # awkward bucket size
    "unit=0&channel=ch2&minutes=120&points=180&end=%d" % END,
    "unit=0&channel=aux3&minutes=120&points=12&end=%d" % END,   # 10 min buckets
    "unit=0&channel=volts&minutes=120&points=60&end=%d" % END,  # the CASE column
    "unit=0&channel=ch1&minutes=120&points=120&agg=max&end=%d" % END,
    "unit=1&channel=ch1&minutes=120&points=120&end=%d" % END,   # a second meter
]


def build_two_hours(con):
    """Two hours of ordinary readings, messy in the ways a real store is messy:
    watts that wander, a channel blanked by the guards, scattered single blanks,
    a meter that loses its own supply so volts collapse below dead_below, and a
    stretch where nothing was recorded at all."""
    rnd = random.Random(1240)
    rows = []
    for unit in (0, 1):
        base = {ch: rnd.uniform(20, 900) for ch in CHANNELS}
        for i in range(1440):                       # 2 h at 5 s
            ts = END - 7200 + i * 5
            if unit == 0 and 400 <= i < 436:        # three minutes not recorded
                continue
            row = {"ts": ts, "unit_id": unit, "volts": round(rnd.uniform(121, 125), 1)}
            if unit == 0 and 700 <= i < 712:        # a meter without its supply
                row["volts"] = round(rnd.uniform(0, 30), 1)
            for ch in CHANNELS:
                base[ch] = max(0.0, base[ch] + rnd.uniform(-40, 40))
                row[ch] = round(base[ch], 1)
            if unit == 0 and 900 <= i < 912:        # a minute blanked by the guards
                row["ch1"] = None
            elif rnd.random() < 0.01:               # scattered single blanks
                row[rnd.choice(CHANNELS)] = None
            rows.append(row)
    con.executemany(
        "INSERT INTO readings (%s) VALUES (%s)"
        % (",".join(COLS), ",".join(":" + c for c in COLS)), rows)
    con.commit()
    return len(rows)


def test_existing_history_does_not_move(fails):
    con = fresh_db(with_dsecs=False)
    n = build_two_hours(con)
    con.close()
    client = api.app.test_client()

    before = {q: get(client, q) for q in QUERIES}
    if api._HAS_DSECS:
        fails.append("has_dsecs() said yes against a store with no dsecs column")
    buckets = sum(len(v["points"]) for v in before.values())

    con = sqlite3.connect(DB)               # the collector's migration, verbatim
    con.execute(ADD_DSECS)
    con.commit()
    con.close()

    after = {q: get(client, q) for q in QUERIES}
    # If this is False the column went unnoticed and `after` is simply `before`
    # again — the comparison below would pass while testing nothing.
    if not api._HAS_DSECS:
        fails.append("the column appeared and the running API did not notice it "
                     "(has_dsecs cached its negative) — the rest of this test "
                     "proves nothing")
    moved, ties = [], 0
    for q in QUERIES:
        span = after[q]["bucket_s"]
        if len(before[q]["points"]) != len(after[q]["points"]):
            moved.append(f"{q}: {len(before[q]['points'])} buckets became "
                         f"{len(after[q]['points'])}")
        for a, b in zip(before[q]["points"], after[q]["points"]):
            if a == b:
                continue
            if (a["ts"] == b["ts"] and a["w"] is not None and b["w"] is not None
                    and is_rounding_tie(q, a["ts"], span, a["w"], b["w"])):
                ties += 1                   # exactly on .05 — see is_rounding_tie
                continue
            moved.append(f"{q}: bucket {a['ts']} read {a['w']}, now reads {b['w']}")
    if moved:
        fails.extend(moved[:8])
        if len(moved) > 8:
            fails.append(f"...and {len(moved) - 8} more buckets moved")
    print(f"  {n} readings, {buckets} buckets across {len(QUERIES)} queries: "
          f"{'every bucket unchanged' if not moved else str(len(moved)) + ' MOVED'}"
          f" when the interval column was added"
          + (f" ({ties} sat exactly on a .05 rounding boundary and drew the other"
             f" way — checked exactly, worth 0.1 W)" if ties else ""))


# ── 2. one long reading must not decide a bucket ────────────────────────────
NORMAL_W, NORMAL_S, NORMAL_N = 100.0, 5, 10
LONG_W, LONG_S = 1000.0, 90                # the reading after a short gap
CAP = api.SNAP_MAX_INTERVAL_S

CAPPED = ((NORMAL_W * NORMAL_S * NORMAL_N + LONG_W * min(LONG_S, CAP))
          / (NORMAL_S * NORMAL_N + min(LONG_S, CAP)))
UNCAPPED = ((NORMAL_W * NORMAL_S * NORMAL_N + LONG_W * LONG_S)
            / (NORMAL_S * NORMAL_N + LONG_S))
PLAIN_AVG = (NORMAL_W * NORMAL_N + LONG_W) / (NORMAL_N + 1)


def test_long_reading_does_not_decide_the_bucket(fails):
    con = fresh_db(with_dsecs=True)
    span = 3600                                 # minutes=60, points=1
    bucket = (END // span) * span
    rows = [{"ts": bucket + 100, "dsecs": LONG_S, "w": LONG_W}]
    rows += [{"ts": bucket + 105 + i * NORMAL_S, "dsecs": NORMAL_S, "w": NORMAL_W}
             for i in range(NORMAL_N)]
    con.executemany(
        "INSERT INTO readings (ts,unit_id,volts,ch1,ch2,aux1,aux2,aux3,aux4,aux5,dsecs)"
        " VALUES (:ts,0,123.4,:w,:w,:w,:w,:w,:w,:w,:dsecs)", rows)
    con.commit()
    con.close()

    client = api.app.test_client()
    d = get(client, f"unit=0&channel=ch1&minutes=60&points=1&end={bucket + span - 1}")
    if len(d["points"]) != 1:
        fails.append(f"expected the eleven readings in ONE bucket, "
                     f"got {len(d['points'])}")
        return
    got = d["points"][0]["w"]
    print(f"  ten {NORMAL_S} s readings at {NORMAL_W:.0f} W plus one {LONG_S} s "
          f"reading at {LONG_W:.0f} W, all in one bucket -> {got} W")
    print(f"    capped at {CAP} s (right): {round(CAPPED, 1)} W · "
          f"uncapped: {round(UNCAPPED, 1)} W · unweighted average: "
          f"{round(PLAIN_AVG, 1)} W")
    if got != round(CAPPED, 1):
        why = (" — the long reading was weighted literally and took the bucket over"
               if got == round(UNCAPPED, 1) else
               " — the weighting is not being applied at all"
               if got == round(PLAIN_AVG, 1) else "")
        fails.append(f"bucket read {got} W, expected {round(CAPPED, 1)} W{why}")


def main():
    fails = []
    print("1. an existing database must read the same")
    test_existing_history_does_not_move(fails)
    print("2. one long reading must not decide a bucket")
    test_long_reading_does_not_decide_the_bucket(fails)
    print("=== RESULT:", "FAIL\n  - " + "\n  - ".join(fails) if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for f in os.listdir(TMP):
            os.remove(os.path.join(TMP, f))
        os.rmdir(TMP)
