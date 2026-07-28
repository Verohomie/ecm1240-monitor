#!/usr/bin/env python3
"""ECM-1240 polled-mode test (Phase 0 fallback tool).

Fallback only: real-time passive listening (ecm_sniff.py) is the default —
field experience shows shared-line collisions are rare and cost only
granularity, since cumulative counters recover the full delta on the next
clean packet. Use this if the sniffer shows sustained packet loss.

Polls one or more ECM-1240 units on a shared serial line. Protocol per
btmon.py / Brultech docs (up to 4 units per line, addressed 0xFC-0xFF):

  send <id byte>  ->  expect 0xFC confirm
  send "SPK"      ->  expect 0xFC confirm
  receive one 65-byte packet (FE FF 03 ... checksum)

Real-time mode must be DISABLED on every unit first (Brultech IA software
or btcfg.py), or free-running packets will collide with poll responses.
Run with -v to hex-dump every byte exchanged — if a unit answers with a
different confirm sequence, the dump shows the real protocol.

Usage:
  python3 tools/ecm_poll.py /dev/ttyUSB0                 # scan FC-FF once
  python3 tools/ecm_poll.py /dev/ttyUSB0 --id fc --loop  # poll one unit forever
  python3 tools/ecm_poll.py --listen 8082 --id fc                   # through a gateway that dialed in
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ecm1240.protocol import (HEADER, PACKET_LEN, add_source_args, checksum_ok,  # noqa: E402
                              open_source, parse_payload, watts)

CONFIRM = 0xFC
TIMEOUT = 1.5


def read_confirm(conn, verbose):
    data = conn.read(1)
    if verbose and data:
        print(f"  << {data.hex()}")
    return len(data) == 1 and data[0] == CONFIRM


def read_packet(conn, verbose):
    buf = bytearray()
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and len(buf) < PACKET_LEN + 8:
        chunk = conn.read(PACKET_LEN)
        if chunk:
            buf += chunk
            deadline = time.time() + 0.3
    if verbose and buf:
        print(f"  << {bytes(buf).hex(' ')}")
    start = buf.find(HEADER)
    if start >= 0 and len(buf) - start >= PACKET_LEN:
        return bytes(buf[start:start + PACKET_LEN])
    return None


def poll(conn, unit_id, verbose):
    conn.reset_input_buffer()
    conn.write(bytes([unit_id]))
    if verbose:
        print(f"  >> {unit_id:02x}")
    if not read_confirm(conn, verbose):
        return None, "no confirm after id byte"
    conn.write(b"SPK")
    if verbose:
        print("  >> SPK")
    if not read_confirm(conn, verbose):
        return None, "no confirm after SPK"
    packet = read_packet(conn, verbose)
    if packet is None:
        return None, "no packet after confirms"
    if not checksum_ok(packet):
        return None, f"bad checksum: {packet.hex()}"
    return parse_payload(packet[3:]), None


def main():
    ap = argparse.ArgumentParser(description="ECM-1240 polled-mode test")
    add_source_args(ap)
    ap.add_argument("--id", help="single command byte to poll (fc, fd, fe, ff); default: scan all")
    ap.add_argument("--loop", action="store_true", help="poll repeatedly (10s cycle, like the collector will)")
    ap.add_argument("-v", "--verbose", action="store_true", help="hex-dump all serial traffic")
    args = ap.parse_args()

    conn = open_source(args)
    ids = [int(args.id, 16)] if args.id else [0xFC, 0xFD, 0xFE, 0xFF]
    prev = {}

    while True:
        for unit_id in ids:
            print(f"polling 0x{unit_id:02x} ...")
            d, err = poll(conn, unit_id, args.verbose)
            if err:
                print(f"  0x{unit_id:02x}: {err}")
                continue
            line = (f"  0x{unit_id:02x}: unit={d['unit_id']} ser={d['ser_no']} "
                    f"volts={d['volts']:.1f} secs={d['secs']} "
                    f"amps={d['ch1_amps']:.2f}/{d['ch2_amps']:.2f}")
            if unit_id in prev:
                w = watts(d, prev[unit_id])
                if w:
                    line += ("  W: ch1={ch1:.0f} ch2={ch2:.0f} aux={a}".format(
                        ch1=w["ch1"], ch2=w["ch2"],
                        a="/".join(f"{w[f'aux{i+1}']:.0f}" for i in range(5))))
            prev[unit_id] = d
            print(line)
            time.sleep(0.5)
        if not args.loop:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
