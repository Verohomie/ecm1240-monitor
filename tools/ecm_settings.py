#!/usr/bin/env python3
"""Read back an ECM-1240's settings block (SET + RCV) — config capture / commissioning tool.

The settings block holds what the streaming packets don't: CT type + range per
channel, PT type/range, packet send interval, unit id, firmware version, serial
number, and the power-change trigger. Captured BEFORE decommissioning the old
units (2026-07-13) so the replacement ECMs can be configured to match.

The RAW HEX is the artifact — decoding is best-effort and the byte map lives in
the legacy ecm2cloud.py (Google Drive). Keep the hex.

Usage (stop the collector first — it owns the port):
  sudo systemctl stop ecm-collector
  python3 tools/ecm_settings.py /dev/ttyUSB0            # scan fc-ff
  python3 tools/ecm_settings.py /dev/ttyUSB0 --id fc
  sudo systemctl start ecm-collector

Safe: sends only the id byte + "SET" + "RCV" (read-only). Never sends TOG/RQS,
so real-time streaming and counters are untouched. A unit that free-runs while
we capture will interleave 65-byte FE FF 03 stream packets into the window;
those are recognized and stripped, and the leftover bytes are the reply.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ecm1240.protocol import HEADER, PACKET_LEN, add_source_args, open_source  # noqa: E402

CONFIRM = 0xFC


def read_for(conn, secs):
    buf = bytearray()
    end = time.time() + secs
    while time.time() < end:
        chunk = conn.read(64)
        if chunk:
            buf += chunk
    return bytes(buf)


def strip_stream_frames(buf):
    """Drop any complete free-running FE FF 03 … packets that landed in the window."""
    out = bytearray(buf)
    removed = 0
    while True:
        i = bytes(out).find(HEADER)
        if i < 0 or len(out) - i < PACKET_LEN:
            break
        del out[i:i + PACKET_LEN]
        removed += 1
    return bytes(out), removed


def capture(conn, uid, verbose):
    conn.reset_input_buffer()
    conn.write(bytes([uid]))
    ack = read_for(conn, 0.8)
    if verbose and ack:
        print(f"  id ack  << {ack.hex(' ')}")
    if CONFIRM not in ack:
        return None, "no confirm after id byte"
    conn.write(b"SET")
    ack2 = read_for(conn, 0.8)
    if verbose and ack2:
        print(f"  SET ack << {ack2.hex(' ')}")
    conn.write(b"RCV")
    raw = read_for(conn, 2.5)
    data, removed = strip_stream_frames(raw)
    data = bytes(b for b in data)
    # leading confirm bytes are protocol noise, not settings
    trimmed = data.lstrip(bytes([CONFIRM]))
    return {"raw": raw, "data": trimmed, "removed": removed,
            "set_ack": CONFIRM in ack2}, None


def main():
    ap = argparse.ArgumentParser(description="ECM-1240 settings capture (SET+RCV)")
    add_source_args(ap)
    ap.add_argument("--id", help="single command byte (fc, fd, fe, ff); default: scan all")
    ap.add_argument("--tries", type=int, default=3, help="attempts per unit (stream collisions)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    conn = open_source(args)
    ids = [int(args.id, 16)] if args.id else [0xFC, 0xFD, 0xFE, 0xFF]

    for uid in ids:
        print(f"\n=== unit cmd byte 0x{uid:02x} ===")
        got = None
        for t in range(args.tries):
            r, err = capture(conn, uid, args.verbose)
            if r and r["data"]:
                got = r
                break
            print(f"  try {t + 1}: {err or 'empty reply'}")
            time.sleep(0.6)
        if not got:
            print("  NO SETTINGS REPLY")
            continue
        d = got["data"]
        print(f"  settings bytes ({len(d)}), {got['removed']} stream frame(s) stripped, "
              f"SET ack={'yes' if got['set_ack'] else 'no'}")
        print("  HEX: " + d.hex(" "))
        print("  DEC: " + " ".join(str(b) for b in d))


if __name__ == "__main__":
    main()
