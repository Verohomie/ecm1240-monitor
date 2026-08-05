#!/usr/bin/env python3
"""Read-only JSON API over the SQLite energy store.

The collector owns all writes. This process opens the database READ-ONLY
(`mode=ro` plus `PRAGMA query_only`), so it can never block or corrupt the
writer, and it works fine against a live WAL.

Endpoints:
  GET /api/config                  -> site name, rate, and the channel list
  GET /api/health                  -> row count, last-sample age, guard tosses
  GET /api/now                     -> latest reading per meter
  GET /api/snapshot                -> the same rows, plus a time-aligned
                                      house / metered / unmetered trio
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
import statistics
import time

from flask import Flask, g, jsonify, request

from . import config as cfgmod
from .protocol import CHANNELS

INSIGHTS_STALE_S = 3600     # 4 missed timer runs -> the app should say so

# ── the aligned snapshot (/api/snapshot) ─────────────────────────────────────
# Two ECMs free-run on their own clocks. There is no way to make them sample
# together, and no setting that syncs them: each simply reports on its own
# interval, so a pair of "latest" readings is typically a second or two apart.
#
# That is enough to break one specific sum: mains minus the total of the metered
# branches, the figure that tells you how much of your house has no CT on it. A
# load switching on between the two meters' reads gets counted on ONE side of
# that subtraction, so the answer briefly jumps by the whole size of the load —
# an oven element looks like a 1.5 kW mystery load for a few seconds, then
# vanishes. Pairing each reading with the closest one in time does NOT fix it:
# the offset between two free-running meters is roughly constant, so the nearest
# neighbour is still the one from a second or two ago.
#
# What does fix it is measuring energy instead of comparing instants. Every
# stored reading is already the AVERAGE power over the interval ending at its
# timestamp (the collector differences the meter's watt-second counters), so
# weighting each reading by how much of its interval falls inside a shared
# window gives the energy that really flowed in that window — the same window
# for every meter, whatever phase each one samples on.
SNAP_WINDOW_S = 30           # shared window; widened below for slow cadences
SNAP_PAD_S = 90              # extra history read so the window's first interval
                             # is complete and a stalled meter is visible
SNAP_DEFAULT_INTERVAL_S = 5  # assumed cadence before any gaps have been seen
SNAP_MAX_INTERVAL_S = 60     # one reading may never stand for more time than this
SNAP_MIN_COVERAGE = 0.6      # less of the window than this = a gap; refuse the sum
SNAP_MAX_SKEW_S = 20         # meters further apart than this: one is stalling

app = Flask(__name__, static_folder=None)
CFG = None                  # populated by main() / create_app()
_HAS_DSECS = False          # latches True once the column appears; see has_dsecs()


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


def has_dsecs():
    """Whether the store carries the per-reading interval column yet.

    The API opens the database read-only, so it cannot add the column itself —
    the collector does that on its next restart. Between upgrading the code and
    restarting the collector, and for any database written by an older release,
    the column is simply absent, and SQL naming it would fail. Falls back to the
    plain average, which is what the older store's equal-length readings meant
    anyway.

    Only a POSITIVE answer is cached. A column never disappears, so once it is
    found the check can stop. A negative has to be re-asked, because the usual
    upgrade order is install the new code, restart the API, restart the
    collector — and it is the collector that adds the column. Caching that first
    "no" would leave the API serving unweighted averages for as long as the
    process runs, silently, with nothing on screen to say so. Re-probing is a
    PRAGMA on an already-open connection, which is not a price worth paying that
    failure mode for.
    """
    global _HAS_DSECS
    if not _HAS_DSECS:
        _HAS_DSECS = any(r[1] == "dsecs"
                         for r in db().execute("PRAGMA table_info(readings)"))
    return _HAS_DSECS


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
        # The dashboard hides the unmetered figure inside this band, and the
        # insights rule judges against the same number — served rather than
        # duplicated in the page, so the screen and the verdict cannot disagree
        # about what counts as noise.
        unmetered_noise_high_w=CFG.get("unmetered", {}).get("noise_high_watts", 100),
        # The service-voltage band, so a voltage chart can draw the in-range
        # region without hardcoding a 120 V service. Same numbers the insights
        # rule judges against — served, not duplicated.
        voltage=CFG.get("voltage", {"low": 114.0, "high": 126.0, "dead_below": 90.0}),
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


def _intervals(rows):
    """Each row's interval length, and the cadence to assume for the first one.

    A row's own `dsecs` is used when it has one: that is the meter's seconds
    counter, the exact number its watts were divided by. The gap between
    timestamps is the fallback, for rows stored before the column existed. The
    two are not interchangeable — a timestamp gap also contains USB/serial
    latency, and the ECM-1240 departs from its own cadence precisely when a large
    load switches, so the fallback mis-sizes the readings that matter most.

    The cadence itself is derived from the data rather than configured: an
    ECM-1240 can be set to report anywhere from about one second to a minute, and
    the right window depends on which. The median observed gap stands in for the
    oldest row, whose own predecessor was not read.
    """
    gaps = [b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])
            if 0 < b["ts"] - a["ts"] <= SNAP_MAX_INTERVAL_S]
    nominal = statistics.median(gaps) if gaps else SNAP_DEFAULT_INTERVAL_S
    out = []
    prev = None
    for r in rows:
        span = r["dsecs"] if "dsecs" in r.keys() and r["dsecs"] else (
            nominal if prev is None else r["ts"] - prev)
        out.append(min(max(span, 1), SNAP_MAX_INTERVAL_S))
        prev = r["ts"]
    return out, nominal


def _window_means(rows, t0, t1):
    """Overlap-weighted mean watts per channel over [t0, t1], plus coverage.

    Each reading is the mean power over the interval ENDING at its timestamp, so
    it is weighted by how much of that interval lies inside the window. The
    result is the energy that actually flowed in [t0, t1] divided by the time it
    flowed over — the same quantity for every meter regardless of when each one
    happened to report.

    Coverage is tracked per channel, so a scrubbed reading (NULL watts, left by
    the consistency guards) lowers that channel's confidence instead of quietly
    dragging its mean toward zero.
    """
    spans, _ = _intervals(rows)
    tot = {ch: 0.0 for ch in CHANNELS}
    cov = {ch: 0.0 for ch in CHANNELS}
    window_cov = 0.0
    for r, span in zip(rows, spans):
        overlap = min(r["ts"], t1) - max(r["ts"] - span, t0)
        if overlap <= 0:
            continue
        window_cov += overlap
        for ch in CHANNELS:
            v = r[ch] if ch in r.keys() else None
            if v is not None:
                tot[ch] += v * overlap
                cov[ch] += overlap
    means = {ch: (tot[ch] / cov[ch] if cov[ch] else None) for ch in CHANNELS}
    return means, window_cov / float(t1 - t0)


@app.get("/api/snapshot")
def snapshot():
    """Latest rows per meter, plus a time-aligned house/metered/unmetered trio.

    `units` is exactly what /api/now returns — kept so a dashboard can drive its
    live tiles from a single request — with each row's age and its `skew_s`
    against the meter carrying the mains stated rather than left implicit.

    `aligned` is the part worth having: house, metered and unmetered watts all
    integrated over ONE window (see SNAP_WINDOW_S above for why comparing the
    latest rows directly cannot be made simultaneous).

    `unmetered_w` is null, with a `reason` in plain English, whenever the sum
    would be dishonest: no mains channel configured, a meter silent, a gap in
    the window, or two meters too far apart to pair. A missing number a
    dashboard can label beats a plausible wrong one.
    """
    now_ts = int(time.time())
    mains = cfgmod.mains_channel(CFG)
    chans = cfgmod.channel_list(CFG)
    units = sorted({c["unit"] for c in chans}) or [0]

    # Anchored on the newest reading in the store, not on the wall clock. If the
    # collector stops, this keeps returning the last coherent picture — with
    # age_s saying how old it is — rather than going blank and looking like a
    # different fault. It is also what lets the endpoint work against recorded
    # data, such as the demo database.
    newest = db().execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    if newest is None:
        return jsonify(ok=False, now=now_ts, units=[],
                       aligned={"unmetered_w": None,
                                "reason": "the database has no readings yet"})
    rows = db().execute("SELECT * FROM readings WHERE ts >= ? ORDER BY ts",
                        (newest - SNAP_WINDOW_S - SNAP_PAD_S,)).fetchall()
    by_unit = {}
    for r in rows:
        by_unit.setdefault(r["unit_id"], []).append(r)
    latest = {u: rs[-1] for u, rs in by_unit.items() if rs}
    if not latest:
        return jsonify(ok=False, now=now_ts, units=[],
                       aligned={"unmetered_w": None,
                                "reason": "no readings to work from"})

    # Anchor on the meter carrying the mains: it is the number everything else
    # is subtracted from, so it is the one the rest should line up with.
    anchor = latest.get(mains[0]) if mains else None
    if anchor is None:
        anchor = max(latest.values(), key=lambda r: r["ts"])

    out_units = []
    for u in sorted(latest):
        d = dict(latest[u])
        d["age_s"] = now_ts - d["ts"]
        d["skew_s"] = d["ts"] - anchor["ts"]
        out_units.append(d)

    present = [u for u in units if u in latest]
    missing = [u for u in units if u not in latest]
    skew = max((abs(latest[u]["ts"] - anchor["ts"]) for u in present), default=0)
    # The window has to END where the SLOWEST meter's data ends, or the mains
    # gets integrated over seconds the branches have not reported yet — which is
    # the very skew this endpoint exists to remove.
    t1 = min((latest[u]["ts"] for u in present), default=anchor["ts"])
    # A slow cadence needs a longer window: the shared window must span several
    # readings on every meter or a single reading dominates it.
    _, nominal = _intervals(by_unit.get(anchor["unit_id"], []))
    window = max(SNAP_WINDOW_S, int(nominal) * 6)
    t0 = t1 - window
    aligned = {"window_s": window, "t0": t0, "t1": t1, "skew_s": skew,
               "unmetered_w": None}

    if not mains:
        aligned["reason"] = ("no channel has role: mains, so there is nothing to "
                             "compare the branches against")
    elif missing:
        aligned["reason"] = ("meter " + "/".join(str(m) for m in missing)
                             + " has not reported")
    elif skew > SNAP_MAX_SKEW_S:
        aligned["reason"] = (f"the meters are {skew} s apart — one of them is "
                             "stalling")
    else:
        means, coverage = {}, {}
        for u in present:
            means[u], coverage[u] = _window_means(by_unit[u], t0, t1)
        aligned["coverage"] = {str(u): round(c, 3) for u, c in coverage.items()}
        thin = [u for u, c in coverage.items() if c < SNAP_MIN_COVERAGE]
        house = means.get(mains[0], {}).get(mains[1])
        if thin:
            aligned["reason"] = ("meter " + "/".join(str(t) for t in thin)
                                 + " has a gap in this window")
        elif house is None:
            aligned["reason"] = "no usable mains reading in this window"
        else:
            # Hidden channels count: hidden means "not shown", not "not
            # measured", and leaving one out would report its circuit as
            # unmetered load.
            metered = sum(means[c["unit"]][c["channel"]] or 0.0 for c in chans
                          if c["role"] != "mains" and c["unit"] in means)
            aligned.update(house_w=round(house, 1),
                           metered_w=round(metered, 1),
                           unmetered_w=round(house - metered, 1))
    return jsonify(ok=True, now=now_ts, ts=anchor["ts"],
                   age_s=now_ts - anchor["ts"], units=out_units, aligned=aligned)


@app.get("/api/history")
def history():
    """Time-bucketed series for one channel of one meter (agg=avg|max).

    `channel` is normally a watt channel. The pseudo-channel 'volts' returns the
    readings table's own line-voltage column instead, so a dashboard can draw
    voltage history and read two meters against each other.

    Samples below voltage.dead_below are dropped from the bucket rather than
    averaged in: a meter that has lost its own supply reads toward zero, and that
    is a dead meter, not a brownout — it must not drag the line down. A bucket
    where every sample was dead comes back null, i.e. an honest gap.
    """
    channel = request.args.get("channel", "ch1")
    agg = request.args.get("agg", "avg")
    if agg not in ("avg", "max"):
        return jsonify(error="agg must be avg or max"), 400
    dead_below = float(CFG.get("voltage", {}).get("dead_below", 90.0))
    if channel == "volts":
        col = f"CASE WHEN volts >= {dead_below} THEN volts END"
    elif channel in CHANNELS:
        col = channel
    else:
        return jsonify(
            error=f"bad channel; pick one of {list(CHANNELS) + ['volts']}"), 400
    if agg == "max":
        value_expr = f"MAX({col})"
    elif not has_dsecs():
        value_expr = f"AVG({col})"     # pre-upgrade store; see has_dsecs()
    else:
        # ENERGY-weighted, not reading-weighted. Each row's watts are the average
        # over the dsecs seconds it covers, so a bucket's true mean power is the
        # energy in it divided by the time: SUM(w*dsecs)/SUM(dsecs). A plain AVG()
        # gives a short reading the same say as a full-length one, and an
        # ECM-1240 emits exactly those short readings at the instant a large load
        # switches — which puts a spike on the chart at every compressor start,
        # sized by how far off cadence the meter slipped rather than by any load.
        #
        # Rows predating the dsecs column hold NULL and fall back to the nominal
        # cadence. Every weight is then equal, which is arithmetically identical
        # to the old AVG(), so history stored before the upgrade reads exactly as
        # it always did. The constant's VALUE only matters in the single bucket
        # that straddles the changeover, where it sets the blend between old and
        # new rows; a bucket either side is unaffected by it.
        #
        # The weight is CAPPED, at the same ceiling the snapshot's spans use. A
        # reading may legitimately cover a long interval: the collector stores
        # any gap up to `guards.rebase_after_s` (120 s by default) before it
        # resyncs instead, so the first reading after a serial hiccup or a
        # collector restart can carry a minute and a half of energy. Weighted
        # literally, that one reading outvotes eighteen ordinary ones and decides
        # whatever bucket it lands in — even though the energy it stands for is
        # spread across several buckets either side of it. Capping stops a
        # gap-adjacent reading dominating a zoomed-in bucket. The old AVG() could
        # not skew this way, so without the cap the weighting would be a
        # regression on exactly the views a gap is most visible in.
        w = f"MIN(COALESCE(dsecs, {SNAP_DEFAULT_INTERVAL_S}), {SNAP_MAX_INTERVAL_S})"
        value_expr = (f"SUM(({col}) * {w}) /"
                      f" NULLIF(SUM(CASE WHEN ({col}) IS NOT NULL THEN {w} END), 0)")
    unit = request.args.get("unit", 0, type=int)
    minutes = max(request.args.get("minutes", 180, type=int), 1)
    points = min(max(request.args.get("points", 180, type=int), 1), 1000)
    # `end` lets a caller ask for a window that does not run up to now, which
    # is what zoom-to-refetch needs: drag onto last Friday evening and re-ask
    # for just that window at full resolution.
    end = request.args.get("end", type=int) or int(time.time())
    since = end - minutes * 60
    span = max(minutes * 60 // points, 1)          # seconds per bucket
    # channel and agg are whitelisted above; span and dead_below are numeric ->
    # safe to inline.
    rows = db().execute(
        f"SELECT (ts/{span})*{span} AS b, {value_expr} AS w"
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
