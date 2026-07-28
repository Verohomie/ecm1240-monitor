#!/usr/bin/env python3
"""Confirm each meter is the one you think it is, from its own data.

WHY THIS EXISTS
---------------
Binding a meter to `/dev/serial/by-path/...` identifies the SOCKET, not the
meter. Move a cable from one USB socket to another — or plug a hub in a
different port — and the collector will confidently apply the wrong unit id.
There is no error and no symptom: one meter's counters simply get subtracted
from the other's, and every watt figure after that is wrong while looking
entirely reasonable.

That failure is invisible precisely because the packets themselves carry no
usable identity (every ECM-1240 reports `unit_id=0`). So the only check
available is behavioural: does the data on each unit still look like the
circuits you assigned to it?

WHAT THIS DOES
--------------
Prints a fingerprint of each configured meter from the recorded data — which
channels are live, their typical and peak draw, and how much each one varies
between day and night — next to the names you gave them in the config.

It deliberately does NOT try to decide for you. Fixed thresholds rot: seasons
change, appliances get replaced, a circuit gets rewired. A human glancing at
"the channel I called Water Heater is idle at 4 W and peaks at 4.3 kW in
bursts" knows instantly whether that is right. A hardcoded rule does not.

    python3 tools/verify_unit_identity.py [--config config.yaml] [--hours 48]

Run it after ANY of: moving a USB cable, adding a hub, a reboot that renamed
ports, or a puzzling change in the numbers.
"""

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ecm1240 import config as cfgmod          # noqa: E402
from ecm1240.protocol import CHANNELS         # noqa: E402

NIGHT = (1, 5)      # hours treated as "quiet" for the day/night comparison


def fmt_w(w):
    if w is None:
        return "     —"
    return f"{w:6.0f}" if w >= 10 else f"{w:6.1f}"


def main():
    ap = argparse.ArgumentParser(
        description="Fingerprint each meter's channels so you can confirm the mapping")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--hours", type=int, default=48,
                    help="how much recent history to summarise (default 48)")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    db_path = cfg["database"]["path"]
    if not os.path.exists(db_path):
        sys.exit(f"no database at {db_path} — has the collector run yet?")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    since = int(time.time()) - args.hours * 3600

    print(f"\nFingerprint of the last {args.hours} h — {cfg['site']['name']}")
    print(f"database: {db_path}\n")

    any_data = False
    for m in cfgmod.meters(cfg):
        unit = m["unit"]
        n = con.execute("SELECT COUNT(*) FROM readings WHERE unit_id=? AND ts>=?",
                        (unit, since)).fetchone()[0]
        print(f"── unit {unit}   port: {m.get('port')}")
        if not n:
            print("   NO DATA in this window. The meter is unplugged, powered off, "
                  "in polled mode (try tools/ecm_realtime.py), or bound to the "
                  "wrong port.\n")
            continue
        any_data = True
        print(f"   {n:,} readings\n")
        print("   channel  name                   idle    typical      peak   "
              "night/day")
        print("   " + "─" * 68)

        for ch in CHANNELS:
            entry = (m.get("channels") or {}).get(ch)
            if not entry:
                continue
            row = con.execute(
                f"SELECT MIN({ch}) lo, AVG({ch}) avg, MAX({ch}) hi"
                " FROM readings WHERE unit_id=? AND ts>=? AND {c} IS NOT NULL"
                .replace("{c}", ch), (unit, since)).fetchone()
            night = con.execute(
                f"SELECT AVG({ch}) FROM readings WHERE unit_id=? AND ts>=?"
                " AND CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INT)"
                " BETWEEN ? AND ?", (unit, since, NIGHT[0], NIGHT[1])).fetchone()[0]
            day = con.execute(
                f"SELECT AVG({ch}) FROM readings WHERE unit_id=? AND ts>=?"
                " AND CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INT)"
                " NOT BETWEEN ? AND ?", (unit, since, NIGHT[0], NIGHT[1])).fetchone()[0]
            ratio = ""
            if night is not None and day:
                ratio = f"{night / day:>7.2f}x" if day > 1 else "      —"
            role = " (mains)" if (entry or {}).get("role") == "mains" else ""
            name = ((entry or {}).get("name") or ch)[:20]
            print(f"   {ch:<8} {name:<20}{fmt_w(row['lo'])} {fmt_w(row['avg'])} "
                  f"{fmt_w(row['hi'])}   {ratio}{role}")
        print()

    if not any_data:
        sys.exit("No data for any meter — nothing to verify.")

    print("What to look for:")
    print("  • A channel you named for a heavy appliance should PEAK high and sit")
    print("    near zero the rest of the time. If it never moves, the CT may be")
    print("    off its conductor, or that name belongs to a different channel.")
    print("  • An always-on circuit should have a steady idle floor and a")
    print("    night/day ratio close to 1.0.")
    print("  • Lighting and living-space circuits should be clearly LOWER at")
    print("    night. A ratio near 1.0 there suggests the channels are swapped.")
    print("  • If two units look like each other's descriptions, your ports have")
    print("    swapped — fix the `port:` values in the config, do not renumber")
    print("    the units, or the stored history stops matching the live data.\n")


if __name__ == "__main__":
    main()
