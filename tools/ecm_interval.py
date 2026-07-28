#!/usr/bin/env python3
"""Set an ECM-1240's packet send frequency (seconds between packets).

Per ECM1240_Command_Set.pdf, "Modifying Packet Send Frequency (1 to 255 s)":

    PC -> FC        ECM -> FC
    PC -> "SET"     ECM -> FC
    PC -> "IV2"     ECM -> FC
    PC -> chr(n)    ECM -> FC        (n = seconds, 1..255)

Run with real-time streaming OFF (tools/ecm_realtime.py --off). A free-running
stream interleaves FE FF 03 packets whose payload bytes can include 0xFC, which
would be misread as an ack. This tool refuses to trust an ack if the port is
still streaming: it drains first and aborts if bytes keep coming.

Reversible: just set the interval back. Touches only the packet cadence — not
the CT/PT calibration, unit id, or the watt-second counters.

Usage (one unit on the line, real-time OFF):
  python3 tools/ecm_realtime.py /dev/ttyUSB1 --off
  python3 tools/ecm_interval.py /dev/ttyUSB1 --seconds 5 -v
  python3 tools/ecm_realtime.py /dev/ttyUSB1 --on
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
    ap = argparse.ArgumentParser(description="ECM-1240 packet send frequency")
    ap.add_argument("port", help="serial device, e.g. /dev/ttyUSB1")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--seconds", type=int, required=True,
                    help="packet send interval, 1..255 (A runs at 5)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not 1 <= args.seconds <= 255:
        sys.exit("--seconds must be 1..255")

    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed: apt install python3-serial")

    conn = serial.Serial(args.port, args.baud, timeout=0.4)

    # If real-time is still on, packet bytes (incl. stray 0xFC in payloads)
    # would corrupt the handshake. Drain, then verify the line is quiet.
    conn.reset_input_buffer()
    time.sleep(1.2)
    if conn.in_waiting:
        conn.reset_input_buffer()
        time.sleep(1.2)
        if conn.in_waiting:
            sys.exit("port is still streaming — run ecm_realtime.py --off first")

    print(f"setting packet interval on {args.port} -> {args.seconds}s")
    conn.write(bytes([CONFIRM]))
    if args.verbose:
        print("  >> fc")
    if not read_ack(conn, "FC", args.verbose):
        sys.exit(1)

    conn.write(b"SET")
    if args.verbose:
        print("  >> SET")
    if not read_ack(conn, "SET", args.verbose):
        sys.exit(1)

    conn.write(b"IV2")
    if args.verbose:
        print("  >> IV2")
    if not read_ack(conn, "IV2", args.verbose):
        sys.exit(1)

    conn.write(bytes([args.seconds]))
    if args.verbose:
        print(f"  >> chr({args.seconds})")
    if not read_ack(conn, f"chr({args.seconds})", args.verbose):
        sys.exit(1)

    print(f"  OK — interval set to {args.seconds}s. Verify with ecm_settings.py "
          "(byte 7 = packet send frequency), then ecm_realtime.py --on.")


if __name__ == "__main__":
    main()
