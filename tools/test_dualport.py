#!/usr/bin/env python3
"""Dual-meter collector test — no hardware required.

Two pseudo-terminals stand in for two USB serial adapters. Both feed packets
carrying `unit_id=0`, exactly as real ECM-1240s do, and the test asserts that
the collector:

  1. tells the two meters apart by WHICH PORT the packet arrived on,
  2. applies each meter's own calibration and nobody else's,
  3. scopes the branch-sum guard to mains meters only.

Point 3 is the subtle one. The branch-sum rule says "the mains cannot exceed
the sum of the branches by more than the unmetered allowance". On a meter whose
ch1 is an ordinary branch circuit that rule is meaningless — a big oven on ch1
with everything else idle would be thrown away as impossible. The test drives
exactly that case.

    python3 tools/test_dualport.py

Exits 0 on success, 1 on failure. Run it after any change to the collector.
"""

import os
import pty
import re
import subprocess
import sys
import tempfile
import time
import tty

HEADER = b"\xfe\xff\x03"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_TEMPLATE = """
site:
  name: "Dual-port test"
database:
  path: "{db}"
api:
  bind: "127.0.0.1"
  port: 8099
collector:
  flush_interval: 3600
  live_feed_port: 0
meters:
  - unit: 0
    port: "{dev_a}"
    baud: 19200
    calibration: {{ ch1: 2.0 }}
    channels:
      ch1:  {{ name: "Mains",    volts: 240, role: "mains" }}
      ch2:  {{ name: "Branch 2", volts: 240 }}
      aux1: {{ name: "Branch 3", volts: 240 }}
      aux2: {{ name: "Branch 4", volts: 120 }}
      aux3: {{ name: "Branch 5", volts: 120 }}
      aux4: {{ name: "Branch 6", volts: 120 }}
      aux5: {{ name: "Branch 7", volts: 120 }}
  - unit: 1
    port: "{dev_b}"
    baud: 19200
    calibration: {{}}
    channels:
      ch1:  {{ name: "Branch 8",  volts: 240 }}
      ch2:  {{ name: "Branch 9",  volts: 240 }}
      aux1: {{ name: "Branch 10", volts: 240 }}
      aux2: {{ name: "Branch 11", volts: 120 }}
      aux3: {{ name: "Branch 12", volts: 120 }}
      aux4: {{ name: "Branch 13", volts: 120 }}
      aux5: {{ name: "Branch 14", volts: 120 }}
unmetered:
  max_watts: 20000
"""


def build(volts, ch1_aws, ch2_aws, secs, aux_ws, ch1_amps, unit_id=0, flag=0):
    """Assemble one valid 65-byte packet with a correct checksum."""
    p = bytearray(62)
    v = round(volts * 10)
    p[0], p[1] = v >> 8, v & 0xFF                      # volts, big-endian
    p[2:7] = ch1_aws.to_bytes(5, "little")
    p[7:12] = ch2_aws.to_bytes(5, "little")
    p[12:17] = ch1_aws.to_bytes(5, "little")           # polarised, unused here
    p[17:22] = ch2_aws.to_bytes(5, "little")
    p[26], p[27] = 0, 0                                # ser_no = 0, like real units
    p[28] = flag
    p[29] = unit_id                                    # ALWAYS 0 on real hardware
    p[30:32] = round(ch1_amps * 100).to_bytes(2, "little")
    p[32:34] = (500).to_bytes(2, "little")
    p[34:37] = secs.to_bytes(3, "little")
    for i, w in enumerate(aux_ws):
        p[37 + 4 * i:41 + 4 * i] = w.to_bytes(4, "little")
    p[59], p[60] = 0xFF, 0xFE
    packet = HEADER + bytes(p[:61])
    return packet + bytes([sum(packet[:64]) & 0xFF])


def stream(fd, profile, steps=4):
    """Write `steps` packets whose counters advance at the profile's watts."""
    ch1_w, ch2_w, aux_w, amps = profile
    ch1 = ch2 = 100_000
    aux = [50_000] * 5
    for n in range(steps):
        os.write(fd, build(123.3, ch1, ch2, 1000 + n * 5, aux, amps))
        time.sleep(0.15)
        ch1 += ch1_w * 5                               # watt-seconds over 5 s
        ch2 += ch2_w * 5
        aux = [a + aux_w[i] * 5 for i, a in enumerate(aux)]


def main():
    ports = []
    for _ in range(2):
        master, slave = pty.openpty()
        tty.setraw(slave)
        tty.setraw(master)
        ports.append((master, os.ttyname(slave)))
    (m_a, dev_a), (m_b, dev_b) = ports
    print(f"port A -> {dev_a} (unit 0)\nport B -> {dev_b} (unit 1)")

    tmpdir = tempfile.mkdtemp(prefix="ecm1240-test-")
    cfg_path = os.path.join(tmpdir, "config.yaml")
    with open(cfg_path, "w") as fh:
        fh.write(CONFIG_TEMPLATE.format(
            db=os.path.join(tmpdir, "test.db"), dev_a=dev_a, dev_b=dev_b))

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "ecm1240.collector",
         "--config", cfg_path, "--dry-run"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(6)      # let BOTH ports finish opening; pyserial flushes on open

    # Meter A: mains 2300 W raw -> expect x2 calibration -> 4600 W.
    # Meter B: 8000 W on ch1 with every other channel idle. ch1 is a BRANCH
    #          here, so the branch-sum rule must not fire and no calibration
    #          may be applied.
    stream(m_a, (2300, 700, [2800, 100, 20, 175, 190], 19.0))
    stream(m_b, (8000, 0, [0, 0, 0, 0, 0], 65.0))
    time.sleep(2)
    proc.terminate()
    out = proc.stdout.read()
    print("\n--- collector output ---\n" + out)

    fails = []
    u0 = re.findall(r"unit 0: [\d.]+V\s+ch1=(\d+)W ch2=(\d+)W aux1=(\d+)W", out)
    u1 = re.findall(r"unit 1: [\d.]+V\s+ch1=(\d+)W", out)
    if not u0:
        fails.append("no unit 0 readings")
    elif u0[-1] != ("4600", "700", "2800"):
        fails.append(f"unit 0 expected ch1=4600 ch2=700 aux1=2800, got {u0[-1]}")
    if not u1:
        fails.append("no unit 1 readings (meter B discarded or misrouted?)")
    elif u1[-1] != "8000":
        fails.append(f"unit 1 expected ch1=8000 (no calibration), got {u1[-1]}")
    if "unit 1: branchsum" in out:
        fails.append("branchsum fired on unit 1 — mains-only rule is not scoped")

    print("=== RESULT:", "FAIL: " + "; ".join(fails) if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
