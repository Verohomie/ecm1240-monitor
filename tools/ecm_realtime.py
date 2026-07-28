#!/usr/bin/env python3
"""ECM-1220/1240 real-time mode toggle (start/stop free-running packets).

A unit with real-time mode OFF is completely silent on the line — it only
answers a direct poll. That is a common state for a meter that looks dead (silent, but
it acked an addressed poll and returned a valid packet). This flips it back to
free-running so the collector can just listen.

Sequence per ECM1240_Packet_format_ver9.pdf ("Real-time Data Start Command"):

    PC  -> FC(hex)        ECM -> FC   (acknowledge)
    PC  -> "TOG"          ECM -> FC   (acknowledge)
    PC  -> "XTD"          ECM begins streaming packets   (ECM-1240 extended)
    PC  -> "OFF"          ECM stops streaming            (same FC/TOG prefix)

Run this with only ONE unit live on the line (or the others quiet) — a
free-running neighbour's packets collide with the FC acks and the handshake
fails. Reversible: whatever --on does, --off undoes.

Usage:
  python3 tools/ecm_realtime.py /dev/ttyUSB0 --on -v
  python3 tools/ecm_realtime.py /dev/ttyUSB0 --off
"""

import argparse
import sys
import time

CONFIRM = 0xFC
ACK_TIMEOUT = 2.0


def read_ack(conn, label, verbose):
    """Wait for the unit's 0xFC acknowledge, tolerating stray bytes."""
    deadline = time.time() + ACK_TIMEOUT
    while time.time() < deadline:
        b = conn.read(1)
        if not b:
            continue
        if verbose:
            print(f"  << {b.hex()}")
        if b[0] == CONFIRM:
            return True
    print(f"  !! no FC acknowledge after {label}")
    return False


def main():
    ap = argparse.ArgumentParser(description="ECM-1240 real-time mode toggle")
    ap.add_argument("port", help="serial device, e.g. /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=19200)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--on", action="store_true", help="start free-running packets (XTD)")
    mode.add_argument("--off", action="store_true", help="stop free-running packets (OFF)")
    ap.add_argument("-v", "--verbose", action="store_true", help="hex-dump the exchange")
    args = ap.parse_args()

    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed: apt install python3-serial")

    conn = serial.Serial(args.port, args.baud, timeout=0.4)
    conn.reset_input_buffer()

    final = b"XTD" if args.on else b"OFF"
    print(f"{'ENABLING' if args.on else 'DISABLING'} real-time on {args.port} @ {args.baud}")

    conn.write(bytes([CONFIRM]))
    if args.verbose:
        print("  >> fc")
    if not read_ack(conn, "FC", args.verbose):
        sys.exit(1)

    conn.write(b"TOG")
    if args.verbose:
        print("  >> TOG")
    if not read_ack(conn, "TOG", args.verbose):
        sys.exit(1)

    conn.write(final)
    if args.verbose:
        print(f"  >> {final.decode()}")

    # No ack is specified after XTD/OFF — the unit just starts (or stops).
    print(f"  sent {final.decode()} — real-time should now be "
          f"{'ON (packets streaming)' if args.on else 'OFF (silent)'}")

    if args.on:
        print("  listening 8s for the stream to start ...")
        time.sleep(8)
        waiting = conn.in_waiting
        print(f"  bytes arrived: {waiting}  -> "
              f"{'STREAMING' if waiting > 0 else 'still silent'}")


if __name__ == "__main__":
    main()
