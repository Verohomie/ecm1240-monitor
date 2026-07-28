#!/usr/bin/env python3
"""Configuration loading and validation.

Everything site-specific lives in one YAML file: which meters exist, which
serial port each is bound to, what the channels are called, calibration
factors, your electricity rate. No circuit name, address or rate is ever
hardcoded in the Python.

Resolution order for the config file:
    1. --config on the command line
    2. $ECM1240_CONFIG
    3. ./config.yaml
    4. /etc/ecm1240/config.yaml

There is deliberately NO built-in fallback configuration. A missing file is a
hard error with instructions, never a silent default — a monitor that quietly
runs with someone else's channel names is worse than one that refuses to start.
"""

import os
import sys

try:
    import yaml
except ImportError:
    yaml = None

from .protocol import CHANNELS

SEARCH_PATHS = ("config.yaml", "/etc/ecm1240/config.yaml")

DEFAULTS = {
    "site": {"name": "My House", "rate_per_kwh": 0.15,
             "currency_symbol": "$", "timezone": "UTC"},
    "database": {"path": "./energy.db"},
    "api": {"bind": "127.0.0.1", "port": 8080, "allowed_origin": ""},
    "collector": {"flush_interval": 30, "live_feed_port": 0, "gateway_port": 8082},
    "unmetered": {"max_watts": 20000},
    "guards": {"max_plausible_watts": 50000, "min_interval_s": 2,
               "rebase_after_s": 120, "coherence_k": 1.3},
    "digest": {"enabled": False},
}


class ConfigError(SystemExit):
    """Fatal, with a message a non-programmer can act on."""


def _merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def find_config(explicit=None):
    if explicit:
        if not os.path.exists(explicit):
            raise ConfigError(f"config file not found: {explicit}")
        return explicit
    env = os.environ.get("ECM1240_CONFIG")
    if env:
        if not os.path.exists(env):
            raise ConfigError(f"ECM1240_CONFIG points at a missing file: {env}")
        return env
    for path in SEARCH_PATHS:
        if os.path.exists(path):
            return path
    raise ConfigError(
        "No configuration file found.\n\n"
        "  cp config.example.yaml config.yaml\n"
        "  ${EDITOR:-nano} config.yaml\n\n"
        f"Searched: {', '.join(SEARCH_PATHS)}, and $ECM1240_CONFIG.\n"
        "There is no default configuration — the channel names and calibration "
        "have to describe YOUR panel, and guessing them would produce readings "
        "that look plausible and are wrong.")


def validate(cfg):
    """Fail loudly at startup rather than subtly at runtime."""
    meters = cfg.get("meters") or []
    if not meters:
        raise ConfigError("config error: no 'meters:' defined. At least one is required.")

    seen_units, seen_ports = {}, {}
    for m in meters:
        if "unit" not in m:
            raise ConfigError("config error: every meter needs a 'unit:' id (0, 1, ...).")
        unit = m["unit"]
        if unit in seen_units:
            raise ConfigError(
                f"config error: unit id {unit} is used twice. Each meter needs its own id — "
                "two meters sharing one id would have their counters subtracted from each "
                "other and produce confidently wrong watts.")
        seen_units[unit] = m

        port = m.get("port")
        if not port:
            raise ConfigError(f"config error: meter unit {unit} has no 'port:'.")
        if "CHANGE-ME" in str(port):
            raise ConfigError(
                f"config error: meter unit {unit} still has the example port "
                f"'{port}'. Set it to your real device — run "
                "`ls -l /dev/serial/by-path/` to find it.")
        if port in seen_ports:
            raise ConfigError(f"config error: port {port} is assigned to two meters.")
        seen_ports[port] = unit

        for ch in (m.get("channels") or {}):
            if ch not in CHANNELS:
                raise ConfigError(
                    f"config error: meter unit {unit} has unknown channel '{ch}'. "
                    f"Valid channels are: {', '.join(CHANNELS)}")
        for ch, factor in (m.get("calibration") or {}).items():
            if ch not in CHANNELS:
                raise ConfigError(
                    f"config error: calibration for unknown channel '{ch}' on unit {unit}.")
            if not isinstance(factor, (int, float)) or factor <= 0:
                raise ConfigError(
                    f"config error: calibration {ch}={factor} on unit {unit} must be a "
                    "positive number.")

    mains = [m["unit"] for m in meters if any(
        (c or {}).get("role") == "mains" for c in (m.get("channels") or {}).values())]
    if len(mains) > 1:
        raise ConfigError(
            f"config error: more than one meter claims a 'role: mains' channel {mains}. "
            "Whole-house totals would be double counted.")
    return cfg


def load(explicit=None):
    if yaml is None:
        raise ConfigError(
            "PyYAML is required to read the config file.\n"
            "  pip install pyyaml         (or: sudo apt install python3-yaml)")
    path = find_config(explicit)
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config file {path} is not valid YAML:\n{e}")
    cfg = _merge(DEFAULTS, raw)
    cfg["_path"] = path
    return validate(cfg)


# ── Convenience accessors ─────────────────────────────────────────────────────

def meters(cfg):
    return cfg.get("meters") or []


def meter(cfg, unit):
    for m in meters(cfg):
        if m["unit"] == unit:
            return m
    return None


def calibration(cfg, unit):
    m = meter(cfg, unit)
    return (m or {}).get("calibration") or {}


def mains_units(cfg):
    """Unit ids with a channel marked `role: mains`.

    The branch-sum guard only means anything for a whole-house channel. On a
    meter whose ch1 is an ordinary branch circuit the rule is nonsense, so it
    must not be applied there.
    """
    out = set()
    for m in meters(cfg):
        for ch in (m.get("channels") or {}).values():
            if (ch or {}).get("role") == "mains":
                out.add(m["unit"])
    return out


def mains_channel(cfg):
    """(unit, channel) of the whole-house mains, or None if not configured."""
    for m in meters(cfg):
        for name, ch in (m.get("channels") or {}).items():
            if (ch or {}).get("role") == "mains":
                return m["unit"], name
    return None


def channel_label(cfg, unit, ch):
    m = meter(cfg, unit) or {}
    entry = (m.get("channels") or {}).get(ch) or {}
    return entry.get("name") or f"{ch} (unit {unit})"


def channel_list(cfg):
    """Flat, ordered list of configured channels for the API and dashboard."""
    out = []
    for m in meters(cfg):
        for ch in CHANNELS:
            entry = (m.get("channels") or {}).get(ch)
            if not entry:
                continue
            out.append({
                "unit": m["unit"],
                "channel": ch,
                "key": ch if m["unit"] == 0 else f"u{m['unit']}_{ch}",
                "name": entry.get("name") or f"{ch} (unit {m['unit']})",
                "volts": entry.get("volts", 120),
                "role": entry.get("role", "branch"),
                "hidden": bool(entry.get("hidden", False)),
                "note": entry.get("note", ""),
            })
    return out


def die(msg):
    print(msg, file=sys.stderr)
    raise SystemExit(2)
