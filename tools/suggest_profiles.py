#!/usr/bin/env python3
"""Derive appliance profiles from YOUR OWN recorded data.

Insight rules need to know, for each circuit, what counts as "running". Those
numbers are specific to your appliances — a 2.6 kW air handler and a 150 W
refrigerator need completely different thresholds, and someone else's values
would produce confident nonsense about your home.

Rather than shipping anyone's thresholds, this reads your database and works
them out, the same way you would by eye:

  1. Build a histogram of each channel's watts.
  2. Find the QUIETEST GAP in that histogram — the empty space between "off"
     and "running". A cycling appliance is strongly bimodal: thousands of
     samples near zero, thousands more up at its running draw, and almost
     nothing in between. That empty middle is where the threshold belongs.
  3. Place the on/off pair either side of the gap, so a load hovering near the
     threshold cannot produce phantom cycles.
  4. Classify the circuit from how it behaves over time:
       always_on   never goes quiet
       cycling     switches on and off many times a day, regular
       burst       switches on and off, but irregular and often idle
       multistate  several distinct running levels, not just one

Run it after a few days of logging — a week is better than a day, because the
rules also want to see a normal weekly pattern.

    python3 tools/suggest_profiles.py > profiles.yaml
    python3 tools/suggest_profiles.py --explain      # show the reasoning

ALWAYS read the result before using it. This is a starting point derived from
statistics, not a diagnosis — it does not know that ch2 is your pool pump, only
that ch2 has two states about 650 W apart. Rename things, fix anything that
looks wrong, and delete circuits you do not care about.
"""

import argparse
import os
import statistics
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ecm1240 import config as cfgmod          # noqa: E402
from ecm1240.protocol import CHANNELS         # noqa: E402

MIN_SAMPLES = 500
BINS = 60


def histogram(vals, lo, hi, bins=BINS):
    if hi <= lo:
        return [len(vals)], (hi - lo) or 1.0
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        i = int((v - lo) / width)
        counts[min(max(i, 0), bins - 1)] += 1
    return counts, width


def find_gap(counts, width, lo):
    """Return the centre of the emptiest run of bins between two populated peaks.

    Looks only at the region between the first and last well-populated bin, so
    the long empty tail above a load's peak draw is never mistaken for the gap.
    """
    total = sum(counts)
    if not total:
        return None
    thresh = total * 0.01
    populated = [i for i, c in enumerate(counts) if c >= thresh]
    if len(populated) < 2:
        return None
    first, last = populated[0], populated[-1]
    if last - first < 3:
        return None                       # single cluster: not bimodal

    best, best_len, run_start = None, 0, None
    for i in range(first + 1, last):
        if counts[i] <= total * 0.002:
            run_start = i if run_start is None else run_start
            run_len = i - run_start + 1
            if run_len > best_len:
                best_len, best = run_len, (run_start, i)
        else:
            run_start = None
    if best is None:
        return None
    centre_bin = (best[0] + best[1]) / 2.0
    return lo + (centre_bin + 0.5) * width, best_len * width


def classify(vals, ts, on_thr, off_thr):
    """Decide the profile kind from behaviour over time."""
    above = [v > on_thr for v in vals]
    frac_on = sum(above) / len(above)
    transitions = sum(1 for a, b in zip(above, above[1:]) if a != b)
    span_h = max((ts[-1] - ts[0]) / 3600.0, 1.0)
    per_h = transitions / 2.0 / span_h

    if frac_on > 0.97:
        return "always_on", per_h
    if frac_on < 0.005:
        return "burst", per_h

    running = [v for v in vals if v > on_thr]
    if running:
        levels = _plateaus(running)
        if len(levels) >= 3:
            return "multistate", per_h
    if per_h >= 0.5:
        return "cycling", per_h
    return "burst", per_h


def _plateaus(running, tol=0.18):
    """Rough count of distinct running levels."""
    running = sorted(running)
    levels, cur = [], [running[0]]
    for v in running[1:]:
        if v <= cur[-1] * (1 + tol) + 15:
            cur.append(v)
        else:
            if len(cur) > len(running) * 0.05:
                levels.append(statistics.median(cur))
            cur = [v]
    if len(cur) > len(running) * 0.05:
        levels.append(statistics.median(cur))
    return levels


def analyse(con, unit, ch, since):
    rows = con.execute(
        f"SELECT ts,{ch} FROM readings WHERE unit_id=? AND ts>=?"
        f" AND {ch} IS NOT NULL ORDER BY ts", (unit, since)).fetchall()
    if len(rows) < MIN_SAMPLES:
        return None, f"only {len(rows)} samples (need {MIN_SAMPLES})"
    ts = [r[0] for r in rows]
    vals = [r[1] for r in rows]

    lo, hi = min(vals), max(vals)
    if hi - lo < 15:
        return None, f"flat at {statistics.median(vals):.0f} W — nothing to segment"

    counts, width = histogram(vals, lo, hi)
    gap = find_gap(counts, width, lo)
    if gap is None:
        med = statistics.median(vals)
        p95 = sorted(vals)[int(len(vals) * 0.95)]
        if p95 - med < 20:
            return None, f"no clear on/off split (steady near {med:.0f} W)"
        centre, gap_w = (med + p95) / 2.0, (p95 - med) * 0.4
    else:
        centre, gap_w = gap

    on_thr = max(5.0, centre + gap_w * 0.25)
    off_thr = max(2.0, centre - gap_w * 0.25)
    if off_thr >= on_thr:
        off_thr = on_thr * 0.6

    kind, per_h = classify(vals, ts, on_thr, off_thr)
    running = [v for v in vals if v > on_thr]
    prof = {
        "unit": unit, "ch": ch, "kind": kind,
        "on": round(on_thr), "off": round(off_thr), "min_dur": 120,
    }
    note = (f"{len(rows):,} samples, {lo:.0f}–{hi:.0f} W, "
            f"~{per_h:.1f} starts/h")
    if kind == "always_on":
        prof["floor_w"] = round(statistics.median(vals))
        prof.pop("on"), prof.pop("off"), prof.pop("min_dur")
        note += f", steady near {prof['floor_w']} W"
    elif kind == "cycling":
        prof.update(short_run_s=300, starts_per_h=max(4, round(per_h * 2)),
                    long_run_h=6.0, duty=True, duty_high=65)
        if running:
            note += f", runs near {statistics.median(running):.0f} W"
    elif kind == "multistate":
        prof["long_run_h"] = 14.0
        note += f", {len(_plateaus(running))} power levels"
    else:
        prof["long_run_h"] = 6.0
    return prof, note


def emit(profiles, notes, cfg, explain):
    out = sys.stdout
    print("# ── Appliance profiles ───────────────────────────────────────────",
          file=out)
    print("#", file=out)
    print("# GENERATED from your own recorded data by tools/suggest_profiles.py",
          file=out)
    print(f"# {time.strftime('%Y-%m-%d %H:%M')} — review before relying on it.",
          file=out)
    print("#", file=out)
    print("# The names come from your config; the numbers come from the data.",
          file=out)
    print("# Nothing here knows what your appliances ARE — only how they behave.",
          file=out)
    print("# Fix anything that looks wrong, and delete circuits you don't care",
          file=out)
    print("# about. Thresholds are watts; min_dur and short_run_s are seconds.",
          file=out)
    print("#", file=out)
    print("#   cycling     switches on/off all day (compressor-like)", file=out)
    print("#   multistate  several legitimate running levels", file=out)
    print("#   burst       human-driven; being idle is normal", file=out)
    print("#   always_on   should hold a steady floor", file=out)
    print("", file=out)
    print("profiles:", file=out)
    for p in profiles:
        name = cfgmod.channel_label(cfg, p["unit"], p["ch"])
        if explain:
            print(f"  # {notes[(p['unit'], p['ch'])]}", file=out)
        print(f"  - unit: {p['unit']}", file=out)
        print(f"    ch: {p['ch']}", file=out)
        print(f'    name: "{name}"', file=out)
        print(f"    kind: {p['kind']}", file=out)
        for k, v in p.items():
            if k in ("unit", "ch", "kind"):
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"      # YAML booleans are lowercase
            print(f"    {k}: {v}", file=out)
        print("", file=out)


def main():
    ap = argparse.ArgumentParser(
        description="Derive appliance profiles from your own recorded data")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--days", type=int, default=7,
                    help="how much history to analyse (default 7)")
    ap.add_argument("--explain", action="store_true",
                    help="annotate each profile with the evidence behind it")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    db_path = cfg["database"]["path"]
    if not os.path.exists(db_path):
        sys.exit(f"no database at {db_path} — has the collector run yet?")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    since = int(time.time()) - args.days * 86400

    profiles, notes, skipped = [], {}, []
    for m in cfgmod.meters(cfg):
        unit = m["unit"]
        for ch in CHANNELS:
            entry = (m.get("channels") or {}).get(ch)
            if not entry:
                continue
            if (entry or {}).get("role") == "mains":
                skipped.append((unit, ch, "whole-house mains — not an appliance"))
                continue
            prof, note = analyse(con, unit, ch, since)
            if prof is None:
                skipped.append((unit, ch, note))
                continue
            profiles.append(prof)
            notes[(unit, ch)] = note

    if not profiles:
        print("# No channel had enough varied data to profile yet.", file=sys.stdout)
        print("# Let the collector run for a few days and try again.", file=sys.stdout)

    emit(profiles, notes, cfg, args.explain)

    for unit, ch, why in skipped:
        print(f"skipped unit {unit} {ch}: {why}", file=sys.stderr)
    print(f"\n{len(profiles)} profiles written, {len(skipped)} skipped.",
          file=sys.stderr)
    print("Review, then save as profiles.yaml next to your config.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
