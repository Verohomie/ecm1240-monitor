#!/usr/bin/env python3
"""Read-only JSON API over the SQLite energy store.

The collector owns all writes. This process opens the database READ-ONLY
(`mode=ro` plus `PRAGMA query_only`), so it can never block or corrupt the
writer, and it works fine against a live WAL.

Endpoints:
  GET /api/config                  -> site name, rate, and the channel list
  GET /api/health                  -> row count, last-sample age, guard tosses
  GET /api/now                     -> latest reading per meter
  GET /api/history?channel=&unit=&minutes=&points=&agg=avg|max&end=
                                   -> time-bucketed watts series
  GET /api/discards?days=          -> consistency-guard rejections over time
  GET /api/insights                -> appliance-health cards (see insights.py)
  GET /                            -> the dashboard

SECURITY
--------
There is NO authentication. The default bind is 127.0.0.1 for that reason.
Anyone who can reach this port can read your household's complete energy
history, which reveals when the house is occupied and when it is empty.
Read SECURITY.md before changing `api.bind`, and do not port-forward it.

Run:  python3 -m ecm1240.api [--config PATH]
"""

import argparse
import json
import os
import sqlite3
import time

from flask import Flask, g, jsonify, request

from . import config as cfgmod
from .protocol import CHANNELS

INSIGHTS_STALE_S = 3600     # 4 missed timer runs -> the app should say so

app = Flask(__name__, static_folder=None)
CFG = None                  # populated by main() / create_app()


def _paths():
    db_path = CFG["database"]["path"]
    base = os.path.dirname(os.path.abspath(db_path))
    return {
        "db": db_path,
        "insights": CFG.get("insights", {}).get(
            "output", os.path.join(base, "insights.json")),
        "history": CFG.get("insights", {}).get(
            "history", os.path.join(base, "insight-history.json")),
    }


def db():
    """Per-request read-only connection to the live SQLite store."""
    if "db" not in g:
        g.db = sqlite3.connect(f"file:{_paths()['db']}?mode=ro", uri=True, timeout=5)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA query_only=ON")
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.after_request
def _cors(resp):
    """Same-origin by default.

    An empty `api.allowed_origin` sends no CORS header at all, so only pages
    served from this same host can read the API. Setting it to "*" lets ANY
    website a household member visits read your energy history through their
    browser — set it only to a specific origin you control.
    """
    origin = (CFG or {}).get("api", {}).get("allowed_origin", "")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
    return resp


@app.errorhandler(sqlite3.Error)
def _db_error(e):
    # Most common cause: the collector has not created the database yet.
    return jsonify(ok=False, error=str(e),
                   hint="Has the collector run yet? It creates the database."), 503


@app.get("/")
def index():
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
    return app.send_static_file("index.html") if app.static_folder else \
        _send_web(web_dir, "index.html")


def _send_web(web_dir, name):
    from flask import send_from_directory
    return send_from_directory(os.path.abspath(web_dir), name)


@app.get("/<path:name>")
def static_files(name):
    web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
    return _send_web(web_dir, name)


def _scalar(sql, args=()):
    """One-value query returning 0 if the table does not exist yet."""
    try:
        return db().execute(sql, args).fetchone()[0] or 0
    except sqlite3.OperationalError:
        return 0


@app.get("/api/config")
def api_config():
    """What the dashboard needs to render itself for THIS installation.

    The circuit list is served rather than hardcoded in the page, so relabelling
    a circuit is a config edit and a refresh — no JavaScript changes.
    """
    site = CFG["site"]
    mains = cfgmod.mains_channel(CFG)
    return jsonify(
        site_name=site.get("name", "My House"),
        rate_per_kwh=site.get("rate_per_kwh", 0.15),
        currency_symbol=site.get("currency_symbol", "$"),
        timezone=site.get("timezone", "UTC"),
        mains={"unit": mains[0], "channel": mains[1]} if mains else None,
        unmetered_label=CFG.get("unmetered", {}).get("label", "Unmetered"),
        channels=cfgmod.channel_list(CFG),
    )


@app.get("/api/health")
def health():
    row = db().execute(
        "SELECT COUNT(*) AS n, MAX(ts) AS last FROM readings").fetchone()
    now_ts = int(time.time())
    return jsonify(
        ok=True, rows=row["n"], last_ts=row["last"], now=now_ts,
        age_s=(now_ts - row["last"]) if row["last"] else None,
        discards_total=_scalar("SELECT COUNT(*) FROM discards"),
        discards_24h=_scalar("SELECT COUNT(*) FROM discards WHERE ts > ?",
                             (now_ts - 86400,)))


@app.get("/api/discards")
def discards():
    """Consistency-guard rejections — how many, why, and the daily pattern.

    A rising count on one rule is how a developing sensor fault announces
    itself before the numbers become obviously wrong.
    """
    now_ts = int(time.time())
    days = min(max(request.args.get("days", 14, type=int), 1), 90)
    since = now_ts - days * 86400
    try:
        by_reason = {r["reason"]: r["n"] for r in db().execute(
            "SELECT reason, COUNT(*) n FROM discards WHERE ts > ? GROUP BY reason",
            (since,)).fetchall()}
        by_day = [{"day": r["d"], "n": r["n"]} for r in db().execute(
            "SELECT date(ts,'unixepoch','localtime') d, COUNT(*) n"
            " FROM discards WHERE ts > ? GROUP BY d ORDER BY d",
            (since,)).fetchall()]
    except sqlite3.OperationalError:
        by_reason, by_day = {}, []
    return jsonify(
        total=_scalar("SELECT COUNT(*) FROM discards"),
        window_days=days,
        window_count=_scalar("SELECT COUNT(*) FROM discards WHERE ts > ?", (since,)),
        last_24h=_scalar("SELECT COUNT(*) FROM discards WHERE ts > ?",
                         (now_ts - 86400,)),
        last_ts=_scalar("SELECT MAX(ts) FROM discards") or None,
        by_reason=by_reason, by_day=by_day)


@app.get("/api/insights")
def insights():
    """Appliance-health findings, served from the file insights.py writes.

    Precomputed deliberately: a full pass takes tens of seconds on a Pi, so
    computing per request would block the API. `stale` tells the app the batch
    job has stopped keeping up, so the UI can say so rather than silently
    presenting an old verdict as current.
    """
    path = _paths()["insights"]
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify(ok=False, pending=True, cards=[],
                       error="No insights computed yet."), 200
    except (OSError, ValueError) as e:
        return jsonify(ok=False, cards=[], error=str(e)), 503
    age = int(time.time()) - data.get("generated_at", 0)
    data.update(ok=True, age_s=age, stale=age > INSIGHTS_STALE_S)
    return jsonify(data)


@app.get("/api/insight-history")
def insight_history():
    """Day-by-day record of every finding. An absent file is a normal state."""
    try:
        with open(_paths()["history"]) as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify(ok=True, findings={}, pending=True), 200
    except (OSError, ValueError) as e:
        return jsonify(ok=False, findings={}, error=str(e)), 503
    data["ok"] = True
    return jsonify(data)


@app.get("/api/now")
def now():
    """Latest reading for every meter."""
    rows = db().execute(
        "SELECT r.* FROM readings r JOIN"
        " (SELECT unit_id, MAX(ts) AS mts FROM readings GROUP BY unit_id) m"
        " ON r.unit_id = m.unit_id AND r.ts = m.mts").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/history")
def history():
    """Time-bucketed watts for one channel of one meter (agg=avg|max)."""
    channel = request.args.get("channel", "ch1")
    if channel not in CHANNELS:
        return jsonify(error=f"bad channel; pick one of {list(CHANNELS)}"), 400
    agg = request.args.get("agg", "avg")
    if agg not in ("avg", "max"):
        return jsonify(error="agg must be avg or max"), 400
    unit = request.args.get("unit", 0, type=int)
    minutes = max(request.args.get("minutes", 180, type=int), 1)
    points = min(max(request.args.get("points", 180, type=int), 1), 1000)
    # `end` lets a caller ask for a window that does not run up to now, which
    # is what zoom-to-refetch needs: drag onto last Friday evening and re-ask
    # for just that window at full resolution.
    end = request.args.get("end", type=int) or int(time.time())
    since = end - minutes * 60
    span = max(minutes * 60 // points, 1)          # seconds per bucket
    # channel and agg are whitelisted above; span is an int -> safe to inline.
    rows = db().execute(
        f"SELECT (ts/{span})*{span} AS b,"
        f" {'MAX' if agg == 'max' else 'AVG'}({channel}) AS w"
        " FROM readings WHERE unit_id = ? AND ts >= ? AND ts <= ?"
        " GROUP BY b ORDER BY b", (unit, since, end)).fetchall()
    # w is NULL when every sample in a bucket was scrubbed (e.g. a mains
    # channel blanked by the guards) -> emit null rather than rounding None.
    return jsonify(
        channel=channel, unit=unit, minutes=minutes, bucket_s=span,
        start=since, end=end, agg=agg,
        points=[{"ts": r["b"], "w": (round(r["w"], 1) if r["w"] is not None else None)}
                for r in rows])


def create_app(cfg):
    global CFG
    CFG = cfg
    return app


def main():
    ap = argparse.ArgumentParser(description="ECM-1240 read-only JSON API")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    create_app(cfg)

    bind = cfg["api"]["bind"]
    port = cfg["api"]["port"]
    if bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: api.bind is {bind} — this API has NO authentication and "
              "will be readable by every device on that network. See SECURITY.md.")
    print(f"serving {cfg['site']['name']} on http://{bind}:{port}")
    app.run(host=bind, port=port, threaded=True)


if __name__ == "__main__":
    main()
