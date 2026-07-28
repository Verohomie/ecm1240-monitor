#!/usr/bin/env python3
"""Passive ECM-1240 packet sniffer — watch a meter talk, live.

The first tool to reach for. Listens on a serial port (or a TCP gateway),
validates checksums, decodes every field and prints one line per packet, plus
derived watts once it has two packets from the same meter.

    python3 tools/ecm_sniff.py /dev/ttyUSB0
    python3 tools/ecm_sniff.py /dev/ttyUSB0 --raw       # hex dump, no parsing
    python3 tools/ecm_sniff.py --tcp 192.0.2.10:8000    # via a gateway
    python3 tools/ecm_sniff.py --listen 8082            # gateway dials in to us

Stop the collector first — only one process can hold a serial port:

    sudo systemctl stop ecm1240-collector

If nothing appears at all, the meter may have real-time mode switched off,
which looks exactly like a dead board. Check with tools/ecm_poll.py and switch
it back on with tools/ecm_realtime.py.
"""

import argparse
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ecm1240.protocol import (HEADER, PACKET_LEN, add_source_args,  # noqa: E402
                              checksum_ok, open_source, parse_payload, watts)


def main():
    ap = argparse.ArgumentParser(description="Passive ECM-1240 packet sniffer")
    add_source_args(ap)
    ap.add_argument("--raw", action="store_true", help="hex-dump everything, no parsing")
    args = ap.parse_args()

    conn = open_source(args)
    print("Watching for ECM-1240 packets ... Ctrl-C to stop")

    if args.raw:
        try:
            while True:
                data = conn.read(64)
                if data:
                    print(data.hex(" "))
        except KeyboardInterrupt:
            return

    buf = bytearray()
    prev_by_unit = {}
    stats = defaultdict(lambda: {"ok": 0, "ser_no": None})
    bad_checksums = 0

    try:
        while True:
            chunk = conn.read(256)
            if not chunk:
                idle = [f"unit {u}: {s['ok']} pkts" for u, s in stats.items()]
                print("... idle 1s" + (f" ({', '.join(idle)})" if idle
                                       else " (no packets yet)"))
                continue
            buf += chunk

            while True:
                start = buf.find(HEADER)
                if start < 0:
                    if len(buf) > 4096:
                        del buf[:-2]              # keep a possible partial header
                    break
                if len(buf) - start < PACKET_LEN:
                    del buf[:start]
                    break

                packet = bytes(buf[start:start + PACKET_LEN])
                del buf[:start + PACKET_LEN]

                if not checksum_ok(packet):
                    bad_checksums += 1
                    print(f"BAD CHECKSUM ({bad_checksums} total): {packet.hex()}")
                    continue

                d = parse_payload(packet[3:])
                unit = d["unit_id"]
                st = stats[unit]
                st["ok"] += 1
                st["ser_no"] = d["ser_no"]

                line = (f"unit={unit} ser={d['ser_no']} volts={d['volts']:.1f} "
                        f"secs={d['secs']} "
                        f"amps={d['ch1_amps']:.2f}/{d['ch2_amps']:.2f} "
                        f"flag=0x{d['flag']:02x}")
                prev = prev_by_unit.get(unit)
                if prev:
                    w = watts(d, prev)
                    if w:
                        aux = "/".join(f"{w[f'aux{i + 1}']:.0f}" for i in range(5))
                        line += (f"  W: ch1={w['ch1']:.0f} ch2={w['ch2']:.0f} "
                                 f"aux={aux}")
                prev_by_unit[unit] = d
                print(line)
    except (KeyboardInterrupt, ConnectionError) as e:
        if isinstance(e, ConnectionError):
            print(f"\n{e} — rerun to wait for the gateway again")
        print("\n--- summary ---")
        for unit, s in sorted(stats.items()):
            print(f"unit {unit}: {s['ok']} good packets, ser_no {s['ser_no']}")
        print(f"bad checksums: {bad_checksums}")
        if len(stats) > 1:
            print("\nNOTE: every ECM-1240 reports unit_id=0, so two meters on one\n"
                  "line cannot be told apart here. Give each its own port and\n"
                  "bind identity in config.yaml — see the README.")


if __name__ == "__main__":
    main()
