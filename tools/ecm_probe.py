#!/usr/bin/env python3
"""One-shot raw-field probe: dump ECM-1240 absolute vs polarized counters,
per-leg amps, secs and the polarity flag so a ch1 mains-inflation event can be
root-caused. Reads the serial port directly; run with the collector stopped.

  python3 tools/ecm_probe.py /dev/ttyUSB0 --seconds 20
"""
import argparse
import sys
import time

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ecm1240.protocol import HEADER, PACKET_LEN, checksum_ok, parse_payload  # noqa

ap = argparse.ArgumentParser()
ap.add_argument("port")
ap.add_argument("--baud", type=int, default=19200)
ap.add_argument("--seconds", type=int, default=20)
a = ap.parse_args()

import serial
conn = serial.Serial(a.port, a.baud, timeout=1)
buf = bytearray()
prev = None
end = time.time() + a.seconds
print("secs dsecs  ch1_aws        ch1_pws        ch2_aws        "
      "ch1_A  ch2_A  flag  V     d_ch1_aws d_ch1_pws")
while time.time() < end:
    data = conn.read(256)
    if data:
        buf += data
    while True:
        s = buf.find(HEADER)
        if s < 0 or len(buf) - s < PACKET_LEN:
            if s < 0 and len(buf) > 4096:
                del buf[:-2]
            break
        pkt = bytes(buf[s:s + PACKET_LEN])
        del buf[:s + PACKET_LEN]
        if not checksum_ok(pkt):
            continue
        d = parse_payload(pkt[3:])
        ds = da = dp = "-"
        if prev:
            ds = d["secs"] - prev["secs"]
            da = d["ch1_aws"] - prev["ch1_aws"]
            dp = d["ch1_pws"] - prev["ch1_pws"]
        print(f"{d['secs']:>6} {str(ds):>5} {d['ch1_aws']:>14} {d['ch1_pws']:>14} "
              f"{d['ch2_aws']:>14} {d['ch1_amps']:>6.2f} {d['ch2_amps']:>6.2f} "
              f"0x{d['flag']:02x}  {d['volts']:.1f}  {str(da):>9} {str(dp):>9}")
        prev = d
conn.close()
