#!/usr/bin/env python3
"""Optional daily email summary.

Sends yesterday's totals, the biggest consumers, estimated cost, and anything
the health rules flagged. Deliberately short — a digest nobody reads is worse
than no digest.

Sending uses Resend (https://resend.com). The API key comes from the
RESEND_API_KEY environment variable and must never be written into config.yaml.

    python3 -m ecm1240.digest --dry-run      # print, don't send
    python3 -m ecm1240.digest

A systemd timer at whatever hour you like is the usual way to run it.
"""

import argparse
import html as htmllib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from . import config as cfgmod

RESEND_URL = "https://api.resend.com/emails"
# Cloudflare in front of some APIs rejects the default python-urllib
# user-agent outright (error 1010), so name ourselves explicitly.
USER_AGENT = "ecm1240-monitor/1.0"


def connect(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA query_only=ON")
    return db


def day_bounds(offset_days=1):
    """Local midnight-to-midnight for `offset_days` ago (1 = yesterday)."""
    now = time.localtime()
    midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                            0, 0, 0, 0, 0, -1))
    end = int(midnight - (offset_days - 1) * 86400)
    return end - 86400, end


def channel_kwh(db, unit, ch, start, end):
    """Energy over a window, from the mean watts and the sample span.

    Averaging then multiplying by the window is right here because samples are
    evenly spaced; it also degrades gracefully across gaps rather than
    inventing energy for time when nothing was recorded.
    """
    row = db.execute(
        f"SELECT AVG({ch}) a, COUNT({ch}) n FROM readings"
        f" WHERE unit_id=? AND ts>=? AND ts<? AND {ch} IS NOT NULL",
        (unit, start, end)).fetchone()
    if not row or row[0] is None or not row[1]:
        return None, 0
    covered = row[1] * 5.0            # nominal 5 s cadence
    return row[0] * covered / 3_600_000.0, row[1]


def build(cfg):
    db_path = cfg["database"]["path"]
    if not os.path.exists(db_path):
        return None
    db = connect(db_path)
    start, end = day_bounds()
    site = cfg["site"]
    rate = site.get("rate_per_kwh", 0) or 0
    sym = site.get("currency_symbol", "$")
    day_label = time.strftime("%A %d %B", time.localtime(start))

    rows = []
    total_kwh = 0.0
    mains = cfgmod.mains_channel(cfg)
    mains_kwh = None

    for c in cfgmod.channel_list(cfg):
        kwh, n = channel_kwh(db, c["unit"], c["channel"], start, end)
        if kwh is None:
            continue
        if c["role"] == "mains":
            mains_kwh = kwh
            continue
        rows.append((c["name"], kwh))
        total_kwh += kwh
    rows.sort(key=lambda r: -r[1])

    house_kwh = mains_kwh if mains_kwh is not None else total_kwh
    unmetered = (house_kwh - total_kwh) if mains_kwh is not None else None

    # Health findings from the last insights pass, if there is one.
    base = os.path.dirname(os.path.abspath(db_path))
    ins_path = cfg.get("insights", {}).get(
        "output", os.path.join(base, "insights.json"))
    findings = []
    try:
        with open(ins_path) as fh:
            data = json.load(fh)
        findings = [c for c in data.get("cards", [])
                    if c.get("level") in ("alert", "watch")]
    except (OSError, ValueError):
        pass

    # Did any meter stop reporting? A silent dropout is the failure most worth
    # surfacing, because everything else still looks normal.
    dropouts = []
    for m in cfgmod.meters(cfg):
        n = db.execute("SELECT COUNT(*) FROM readings WHERE unit_id=? AND ts>=? AND ts<?",
                       (m["unit"], start, end)).fetchone()[0]
        expected = 86400 / 5
        if n < expected * 0.5:
            pct = 100.0 * n / expected
            dropouts.append((m["unit"], n, pct))

    return {
        "site": site.get("name", "Home"), "day": day_label,
        "house_kwh": house_kwh, "unmetered": unmetered,
        "cost": house_kwh * rate if rate else None, "sym": sym, "rate": rate,
        "rows": rows, "findings": findings, "dropouts": dropouts,
        "unmetered_label": cfg.get("unmetered", {}).get("label", "Unmetered"),
    }


def render_text(d):
    L = [f"{d['site']} — {d['day']}", ""]
    L.append(f"Whole house: {d['house_kwh']:.1f} kWh"
             + (f"  ≈ {d['sym']}{d['cost']:.2f}" if d["cost"] is not None else ""))
    if d["unmetered"] is not None and d["unmetered"] > 0.05:
        L.append(f"  of which {d['unmetered']:.1f} kWh was {d['unmetered_label'].lower()}")
    L.append("")
    L.append("Biggest consumers:")
    for name, kwh in d["rows"][:8]:
        share = (100.0 * kwh / d["house_kwh"]) if d["house_kwh"] else 0
        cost = f"  {d['sym']}{kwh * d['rate']:.2f}" if d["rate"] else ""
        L.append(f"  {name:<22} {kwh:6.2f} kWh  {share:4.0f}%{cost}")
    if d["findings"]:
        L.append("")
        L.append("Worth a look:")
        for c in d["findings"]:
            L.append(f"  [{c['level'].upper()}] {c['title']}")
            L.append(f"      {c['detail']}")
    if d["dropouts"]:
        L.append("")
        L.append("Data gaps:")
        for unit, n, pct in d["dropouts"]:
            L.append(f"  Meter {unit} reported only {pct:.0f}% of expected samples "
                     f"({n:,}). Check the cable, the adapter and the power supply.")
    return "\n".join(L)


def render_html(d):
    e = htmllib.escape
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;'
        'font-size:16px;color:#16222e;max-width:640px">',
        f'<h2 style="margin:0 0 2px">{e(d["site"])}</h2>',
        f'<div style="color:#5a6b7c;font-size:15px">{e(d["day"])}</div>',
        '<div style="background:#f4f7fa;border-radius:10px;padding:16px;'
        'margin:16px 0;text-align:center">',
        f'<div style="font-size:34px;font-weight:700">{d["house_kwh"]:.1f} kWh</div>',
    ]
    if d["cost"] is not None:
        parts.append(f'<div style="color:#5a6b7c">≈ {e(d["sym"])}{d["cost"]:.2f} '
                     f'at {e(d["sym"])}{d["rate"]}/kWh</div>')
    if d["unmetered"] is not None and d["unmetered"] > 0.05:
        parts.append(f'<div style="color:#5a6b7c;font-size:14px;margin-top:6px">'
                     f'{d["unmetered"]:.1f} kWh {e(d["unmetered_label"].lower())}</div>')
    parts.append("</div>")

    parts.append('<table style="width:100%;border-collapse:collapse;font-size:15px">')
    for name, kwh in d["rows"][:8]:
        share = (100.0 * kwh / d["house_kwh"]) if d["house_kwh"] else 0
        parts.append(
            f'<tr><td style="padding:7px 0;border-bottom:1px solid #e4eaf0">{e(name)}</td>'
            f'<td style="padding:7px 0;border-bottom:1px solid #e4eaf0;text-align:right">'
            f'<b>{kwh:.2f}</b> kWh</td>'
            f'<td style="padding:7px 0 7px 14px;border-bottom:1px solid #e4eaf0;'
            f'text-align:right;color:#5a6b7c">{share:.0f}%</td></tr>')
    parts.append("</table>")

    for c in d["findings"]:
        colour = "#c0392b" if c["level"] == "alert" else "#b26a00"
        parts.append(
            f'<div style="border-left:4px solid {colour};background:#fafcfe;'
            f'padding:10px 14px;margin-top:12px">'
            f'<b>{e(c["title"])}</b><br>'
            f'<span style="color:#5a6b7c;font-size:14px">{e(c["detail"])}</span></div>')

    for unit, n, pct in d["dropouts"]:
        parts.append(
            f'<div style="border-left:4px solid #c0392b;background:#fff5f5;'
            f'padding:10px 14px;margin-top:12px">'
            f'<b>Meter {unit} reported only {pct:.0f}% of expected samples.</b><br>'
            f'<span style="color:#5a6b7c;font-size:14px">Check the cable, the USB '
            f'adapter and the 12 VAC supply.</span></div>')

    parts.append('<div style="color:#8496a6;font-size:13px;margin-top:22px">'
                 'ecm1240-monitor</div></div>')
    return "".join(parts)


def send(cfg, d, dry_run):
    text = render_text(d)
    if dry_run:
        print(text)
        return True
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        print("RESEND_API_KEY is not set — cannot send.", file=sys.stderr)
        return False
    dg = cfg.get("digest", {})
    payload = {
        "from": dg.get("from", "Energy Monitor <noreply@example.com>"),
        "to": [dg.get("to")],
        "subject": f"{d['site']} — {d['house_kwh']:.1f} kWh {d['day']}",
        "text": text,
        "html": render_html(d),
    }
    req = urllib.request.Request(
        RESEND_URL, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ok = 200 <= resp.status < 300
            print("sent" if ok else f"HTTP {resp.status}")
            return ok
    except urllib.error.HTTPError as ex:
        print(f"send failed: HTTP {ex.code} {ex.read()[:300]!r}", file=sys.stderr)
    except (urllib.error.URLError, OSError) as ex:
        print(f"send failed: {ex}", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser(description="Daily energy email digest")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the digest instead of sending it")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    if not cfg.get("digest", {}).get("enabled") and not args.dry_run:
        print("digest.enabled is false — nothing to do.")
        return
    d = build(cfg)
    if d is None:
        print("no database yet — nothing to summarise.", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0 if send(cfg, d, args.dry_run) else 1)


if __name__ == "__main__":
    main()
