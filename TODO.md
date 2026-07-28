# TODO

Open items for ecm1240-monitor.

- [ ] **Test against a second person's panel.** Everything so far has been
      validated against one installation plus synthetic data. The first real
      report from someone else's wiring is the one that matters.
- [ ] **Support a single-meter install end to end.** The code handles it, but
      no one has run it that way yet — the dashboard's whole-house fallback
      (sum of branches when there is no mains channel) needs a real trial.
- [ ] **Let the dashboard show two meters as one house.** Right now every
      circuit is listed flat; grouping by meter or by room would help anyone
      with fourteen channels.
- [ ] **Add a hover/touch readout on the chart** showing the value at the
      pointer. Zoom and pan are in; a crosshair with a figure is the obvious
      next step.
- [ ] **Add a `--once` mode to the collector** so people can confirm wiring
      works without leaving a service running.
- [ ] **Improve `suggest_profiles.py` for multistate loads.** It finds the
      on/off split well; the plateau detection for variable-speed equipment is
      cruder and tends to under-count levels.
- [ ] **Document the serial-to-Ethernet gateway path properly.** The code
      supports `--gateway` and `--tcp`, but the docs only cover USB serial.
- [ ] **Check Python 3.9 compatibility on a real Pi.** Developed on a newer
      interpreter; nothing obviously version-specific, but it is untested.

## Done

- [x] Independent ECM-1240 protocol implementation, verified against hardware (2026-07-28)
- [x] Port-based meter identity, so two meters reporting `unit_id=0` stay distinct (2026-07-28)
- [x] Consistency guards with quarantine and mains-only salvage (2026-07-28)
- [x] Config-driven circuits, calibration and rates — no house hardcoded in the source (2026-07-28)
- [x] Read-only API defaulting to localhost, with same-origin CORS (2026-07-28)
- [x] Dashboard that renders whatever `/api/config` describes (2026-07-28)
- [x] Insights engine with profiles derived from the user's own data (2026-07-28)
- [x] Demo mode — full stack runnable with no hardware (2026-07-28)
- [x] Dual-meter test using pseudo-terminals (2026-07-28)
- [x] Install, security, calibration and troubleshooting docs (2026-07-28)
- [x] Drag-to-zoom charts that re-fetch at full resolution, in dependency-free SVG (2026-07-28)
