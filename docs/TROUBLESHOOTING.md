# Troubleshooting

> [!IMPORTANT]
> **Start here: a silent meter is usually not a broken meter.**
> An ECM-1240 with real-time mode switched off never sends a packet, but still
> answers a direct poll. It is indistinguishable from dead hardware, and it is
> the most common problem people hit. Two commands rule it out — see the first
> section below before doing anything else.

## The meter looks completely dead

**Check this before you suspect the hardware.** An ECM-1240 with real-time mode
switched off is totally silent on the line — no packets, ever — but it is alive
and will answer a direct poll. It looks identical to a dead board. People have
replaced perfectly good meters over this.

```bash
python3 tools/ecm_poll.py /dev/ttyUSB0 -v      # does it answer a poll?
```

If it answers, real-time mode is off. Turn it back on:

```bash
python3 tools/ecm_realtime.py /dev/ttyUSB0 --on
```

If it does **not** answer a poll either, work through:

1. **Power.** Is the 12 VAC transformer plugged in and live? The meter has no
   battery, and the transformer is also its voltage reference.
2. **The adapter.** `ls -l /dev/serial/by-path/` — does the device exist?
   `dmesg | tail` after unplugging and replugging shows whether the kernel sees
   it at all.
3. **Wiring.** RS-232 needs the right pins. A crossed TX/RX gives exactly this
   symptom: a link that looks connected and never speaks.
4. **Another program has the port.** Only one process can hold a serial port.
   `sudo systemctl stop ecm1240-collector` before running any bench tool.

## Watts are wrong

See [`CALIBRATION.md`](CALIBRATION.md) — start with `tools/ecm_settings.py` to
read back what CT type and range the meter thinks it has.

## Two meters, and the numbers are nonsense

Almost certainly a **port identity swap**. Both meters report `unit_id=0`, so
identity comes from which port each is plugged into. Move a cable to a different
USB socket — or add a hub — and the collector applies the wrong unit id. One
meter's counters get subtracted from the other's.

```bash
python3 tools/verify_unit_identity.py
```

That prints a fingerprint of each meter's channels. If unit 0 now looks like the
circuits you assigned to unit 1, your ports have swapped.

**Fix the `port:` values in `config.yaml` to match reality — do not renumber the
units.** Renumbering makes the live data disagree with everything already stored.

Use `/dev/serial/by-path/` names, never `/dev/ttyUSB0`, or this will recur at
every reboot.

## The "Unmetered" figure is large, or jumps around

Unmetered is the mains minus every metered branch: whatever is on your service
that no CT is clamped around. Three different things look alike here.

**It flashes a big number for a few seconds when something switches on.** That
was the old behaviour and is fixed — the dashboard now reads `/api/snapshot`,
which measures both meters over one shared window instead of subtracting two
readings taken a second or two apart. If you still see it, you are on an older
API (the page falls back to `/api/now` silently) or a browser cache: reload with
Ctrl-Shift-R, and check `curl localhost:8080/api/snapshot` returns an `aligned`
block.

**It sits at a steady few hundred watts and never moves.** Something real is
uncounted. Two candidates, in order of likelihood:

1. **A CT has come off**, or is clamped around a cable that carries two
   conductors in opposite directions (a whole cable rather than one conductor),
   in which case it reads near zero while the circuit is definitely running.
   Compare each branch against the appliance you know is on it.
2. **A circuit genuinely has no CT** — a subpanel, an EV charger, an outbuilding
   added after the meters went in. That is not a fault. Raise
   `unmetered.noise_high_watts` above it, or clamp it and add the channel.

**It goes negative — the branches add up to more than the mains.** Every watt
has to cross the mains CT to reach a branch, so this is always a measurement
error: two CTs on the same circuit, a branch CT programmed with the wrong range,
or a mains channel reading low. See [`CALIBRATION.md`](CALIBRATION.md).

The insights pass reports on this once a day's data exists, judging the *median*
and the quietest tenth rather than the peak — so a big occasional load (that EV
charger) cannot trip it, while a CT that has come off cannot hide.

## Readings stop for hours, then come back

Look for guard rejections:

```bash
curl -s localhost:8080/api/discards | python3 -m json.tool
```

If `branchsum` dominates, the mains exceeded the sum of the metered branches by
more than `unmetered.max_watts`, and readings were rejected as impossible.

**The usual cause is a real, large, unmetered load** — most often an EV charger
or a subpanel added after the meters were installed. A 60 A charger draws about
14 kW, so a low ceiling rejects genuine readings for as long as it charges.
Raise `unmetered.max_watts` above your largest unmetered load.

If `coherence` dominates, a channel reported more watts than volts × amps allows.
That is a genuine sensor or counter fault, not a configuration problem.

## Gaps in the history

The collector deliberately leaves an honest gap rather than inventing data:

- A gap longer than `guards.rebase_after_s` (default 120 s) causes a resync
  instead of one smeared reading spanning the whole outage.
- Segments either side of a data gap are never merged, so an outage cannot turn
  two ordinary runs into one phantom marathon run.

Frequent short gaps usually mean a flaky USB adapter. Cheap PL2303 clones are
the common culprit; an FTDI-based adapter is markedly more reliable.

## The dashboard says "cannot reach the API"

```bash
python3 -m ecm1240.api           # is it running? what does it print?
curl -s localhost:8080/api/health
```

If the API reports a database error, the collector has probably not created the
database yet — start it first and give it a minute.

If the dashboard works locally but not from another device, that is
`api.bind: 127.0.0.1` doing its job. Read [`SECURITY.md`](SECURITY.md) before
changing it.

## Insights show nothing

Insights need `profiles.yaml`, and there are no defaults — someone else's
thresholds would produce confident nonsense about your appliances.

```bash
python3 tools/suggest_profiles.py --explain > profiles.yaml
python3 -m ecm1240.insights
```

If `suggest_profiles.py` skips most channels, it usually needs more history.
Give it a few days and run it again; a week is better than a day.

## Everything is broken and I want to start over

The database is the only irreplaceable thing. Back it up first:

```bash
sqlite3 energy.db ".backup 'energy-backup.db'"
```

`.backup` is safe on a live database; copying the file while the collector is
writing is not.

## Verifying a change did not break anything

```bash
python3 tools/test_dualport.py
```

Simulates two meters with pseudo-terminals and checks that port-based identity,
per-meter calibration and the guard scoping all still work. No hardware needed.
Run it after any change to the collector.
