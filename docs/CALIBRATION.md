# Calibration — getting the numbers right

An energy monitor that is wrong looks exactly like one that is right. There is
no error message, no gap in the graph — just plausible numbers that happen to
be false. This page is about the ways that happens, and how to check.

## The most common problem: CT type and range

The ECM-1240 does not know what current transformer you clamped onto a wire.
You tell it, by programming a **CT type** and **range** per channel. If those
settings do not match the CT physically on that channel, the meter reports a
consistent fraction or multiple of the real current — forever, and silently.

Read back what your meter actually thinks:

```bash
sudo systemctl stop ecm1240-collector      # the collector owns the port
python3 tools/ecm_settings.py /dev/ttyUSB0
sudo systemctl start ecm1240-collector
```

This prints the stored CT type/range for every channel, the PT (voltage
reference) type/range, firmware version and unit id. Compare it against the CT
model actually on each conductor.

A frequent real-world case: mains CTs programmed with a **SPLIT-100** setting
while **SPLIT-200** clamps are on the service conductors. The channel then
reports almost exactly **half** the true whole-house power — a number that
looks entirely believable.

## Verifying against a clamp meter

The only way to know is to measure independently.

1. Get a **true-RMS clamp meter**.
2. Pick a channel and create a large, steady load on it (an electric kettle, a
   space heater — something that draws hundreds of watts and holds steady).
3. Clamp the meter around the same conductor the CT is on and read the amps.
4. Compare against what the dashboard shows for that channel.
5. For a **whole-house mains channel on a split-phase service, measure BOTH
   legs and add them.** Comparing one leg against a dual-channel total is how
   people convince themselves of a factor-of-two error that is not there.

Do this at two different load levels — say 5 A and 30 A. A CT that is right at
one level and wrong at another has a different problem (wrong range, or a
clamp not fully closed) than one that is off by a constant factor.

## Correcting an error — pick ONE place

If you find a constant factor, you can fix it in **either** of two places:

**A. In the meter** — reprogram the CT type/range so the meter itself reports
correctly. Cleanest, and it fixes every consumer of the data.

**B. In this software** — a `calibration:` entry in `config.yaml`:

```yaml
meters:
  - unit: 0
    calibration: { ch1: 2.0 }
```

### ⚠ NEVER BOTH

This is the single most expensive mistake available here.

If you correct the CT range in the meter **and** leave a `calibration:` factor
in the config, the correction is applied twice. A channel that was reading half
now reads **double** — a four-fold error from where you started, and it will
still look like a plausible house.

**If you reprogram the meter, delete the calibration entry in the same sitting.**
Do not leave it "just in case". The config file ships with `calibration: {}`
empty precisely so nobody inherits someone else's factor.

## Sanity checks that catch a bad calibration

**Does the mains roughly equal the sum of the branches?**

If you have a mains channel and CTs on most circuits, the mains should be a
little higher than the branch sum — the difference being circuits with no CT.
If the mains is *lower* than the sum of its own branches, something is wrong.

The collector enforces a version of this automatically. See `unmetered.max_watts`
in the config: it sets how far the mains may legitimately exceed the branch sum
before a reading is treated as a fault.

**Set that value higher than your largest unmetered load.** An EV charger on a
60 A circuit draws around 14 kW, so a 7 kW ceiling would reject every genuine
reading for hours while the car charges — and because the guard drops the whole
packet, it can freeze an entire meter. If your mains readings vanish at the same
time every evening, this is the first thing to check.

**Does the total match your utility bill?**

Over a full billing period, whole-house kWh should land within a few percent of
the utility's figure. This is the best end-to-end check available, and it needs
no equipment. It will not catch a per-channel error that cancels out, but it
reliably catches a wrong mains.

**Does an appliance match its nameplate?**

A resistive load is honest: a 1500 W heater should read close to 1500 W. If a
channel reads 750 W or 3000 W for it, you have found your factor.

## Things that are not calibration errors

- **Watts derived from cumulative counters.** The meter never sends watts; this
  software divides the change in a watt-second counter by the change in the
  seconds counter. A brief spike after a gap in the data is a timing artifact,
  not a bad CT.
- **A channel reading a few watts when everything is off.** Small residual
  readings at the bottom of a CT's range are normal. If several idle channels
  on one meter move together, that is common-mode noise, not real load —
  do not add them up and do not treat the sum as real.
- **Voltage reading slightly off between two meters.** Each meter measures
  voltage through its own transformer; a volt or two of difference between two
  units is normal and does not affect watt accuracy meaningfully.
