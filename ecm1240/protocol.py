#!/usr/bin/env python3
"""ECM-1240 binary protocol — packet framing, decoding, and watt derivation.

Independent implementation of the Brultech ECM-1240 real-time packet format.
Field offsets follow Brultech's published specification (ECM1240_Packet_format
ver 9, available from brultech.com), verified against live hardware and
cross-checked against the community `ecm2cloud.py` script. No third-party code
is included here — see CREDITS.md for the people whose prior work on this
protocol made it straightforward to verify.

The wire format, in brief:

    FE FF 03  <62-byte payload>  <checksum>          = 65 bytes total

Counters are cumulative and little-endian (LSB first); voltage is the one
big-endian field. Watts are never transmitted — they are DERIVED from the
change in a watt-second counter divided by the change in the meter's own
seconds counter. That is why two consecutive packets are always required
before any power reading can be produced.

Every counter wraps, at a different width:
    watt-seconds (ch1/ch2)  2^40      seconds counter   2^24
    watt-seconds (aux1-5)   2^32

A wrap looks exactly like a negative delta, so each is corrected by adding its
own modulus. Getting a width wrong produces a single enormous spike rather than
a steady error, which is why they are named explicitly rather than shared.
"""

import socket
import sys

try:
    import serial
except ImportError:
    serial = None  # decoding stays importable; only the serial path needs it

BAUD = 19200
HEADER = b"\xfe\xff\x03"
PACKET_LEN = 65          # 3 header + 62 payload
PAYLOAD_LEN = 62

CHANNELS = ("ch1", "ch2", "aux1", "aux2", "aux3", "aux4", "aux5")

# Counter widths, per Brultech's published packet format.
WRAP_WS_MAIN = 1 << 40   # ch1/ch2 watt-second counters are 5 bytes
WRAP_WS_AUX = 1 << 32    # aux1-5 watt-second counters are 4 bytes
WRAP_SECS = 1 << 24      # the meter's seconds counter is 3 bytes


def le(data):
    """Little-endian bytes -> int (ECM counters are LSB first)."""
    return int.from_bytes(data, "little")


def parse_payload(p):
    """Decode the 62-byte payload per Brultech's published packet format."""
    return {
        "volts": 0.1 * (p[0] * 256 + p[1]),           # big-endian, unlike counters
        "ch1_aws": le(p[2:7]),                        # absolute watt-seconds, 5 bytes
        "ch2_aws": le(p[7:12]),
        "ch1_pws": le(p[12:17]),                      # polarized watt-seconds
        "ch2_pws": le(p[17:22]),
        "ser_no": p[26] * 256 + p[27],
        "flag": p[28],                                # reset/polarity flags
        "unit_id": p[29],
        "ch1_amps": 0.01 * le(p[30:32]),
        "ch2_amps": 0.01 * le(p[32:34]),
        "secs": le(p[34:37]),
        "aux_ws": [le(p[37 + 4 * i:41 + 4 * i]) for i in range(5)],
    }


def checksum_ok(packet):
    """The trailing byte is the low byte of the sum of all preceding bytes."""
    return sum(packet[:64]) & 0xFF == packet[64]


def watts(now, prev):
    """Average watts per channel between two packets from the SAME meter.

    Passing packets from two different meters here yields confident nonsense,
    not an error — the counters simply subtract. Callers must key their
    previous-packet state by unit id.
    """
    dsecs = now["secs"] - prev["secs"]
    if dsecs <= 0:
        dsecs += WRAP_SECS
    if dsecs <= 0:
        return None
    out = {}
    for ch in ("ch1_aws", "ch2_aws"):
        d = now[ch] - prev[ch]
        if d < 0:
            d += WRAP_WS_MAIN
        out[ch.replace("_aws", "")] = d / dsecs
    for i in range(5):
        d = now["aux_ws"][i] - prev["aux_ws"][i]
        if d < 0:
            d += WRAP_WS_AUX
        out[f"aux{i + 1}"] = d / dsecs
    return out


def framed(source, on_bad=None):
    """Yield checksum-good 65-byte packets from one byte source.

    The buffer is deliberately PER-SOURCE. Two meters interleaved into a single
    buffer would frame one meter's header onto the other's payload; every
    checksum would fail and no packet would ever be produced. One buffer per
    reader thread is not an optimisation, it is a correctness requirement.
    """
    buf = bytearray()
    for chunk in source:
        buf += chunk
        while True:
            start = buf.find(HEADER)
            if start < 0:
                if len(buf) > 4096:
                    del buf[:-2]                      # keep a possible partial header
                break
            if len(buf) - start < PACKET_LEN:
                del buf[:start]
                break
            packet = bytes(buf[start:start + PACKET_LEN])
            del buf[:start + PACKET_LEN]
            if not checksum_ok(packet):
                if on_bad:
                    on_bad()
                continue
            yield packet


# ── Transports ────────────────────────────────────────────────────────────────

class TcpConn:
    """Wraps a socket to match the serial.Serial read()/write() contract."""

    def __init__(self, sock):
        self.sock = sock
        self.sock.settimeout(1)

    def read(self, n):
        try:
            data = self.sock.recv(n)
        except socket.timeout:
            return b""
        if data == b"":
            raise ConnectionError("peer closed the connection")
        return data

    def write(self, data):
        self.sock.sendall(data)

    def reset_input_buffer(self):
        self.sock.settimeout(0.05)
        try:
            while self.sock.recv(4096):
                pass
        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(1)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def open_source(args):
    """Serial port, outbound TCP, or listening server per parsed args."""
    if getattr(args, "listen", None):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", args.listen))
        srv.listen(1)
        print(f"Listening on 0.0.0.0:{args.listen} — waiting for a gateway to connect ...")
        sock, addr = srv.accept()
        print(f"Gateway connected from {addr[0]}:{addr[1]}")
        return TcpConn(sock)
    if getattr(args, "tcp", None):
        host, _, port = args.tcp.rpartition(":")
        print(f"Connecting to {host}:{port} ...")
        return TcpConn(socket.create_connection((host, int(port)), timeout=5))
    if not getattr(args, "port", None):
        sys.exit("give a serial port, --tcp host:port, or --listen port")
    if serial is None:
        sys.exit("pyserial not installed: pip install pyserial")
    return serial.Serial(args.port, args.baud, timeout=1)


def add_source_args(ap):
    ap.add_argument("port", nargs="?",
                    help="serial device, e.g. /dev/ttyUSB0 or /dev/serial/by-path/...")
    ap.add_argument("--tcp", metavar="HOST:PORT",
                    help="connect out to a serial-to-Ethernet gateway in server mode")
    ap.add_argument("--listen", type=int, metavar="PORT",
                    help="act as the server a gateway client dials into")
    ap.add_argument("--baud", type=int, default=BAUD)
