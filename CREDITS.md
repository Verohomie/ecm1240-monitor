# Credits

## Origin and independence

This project is an **independent implementation** of the Brultech ECM-1240
protocol. It contains **no third-party code**.

The packet format is decoded from Brultech's own published specification
(`ECM1240_Packet_format` ver 9, and `ECM1240_Command_Set`, both available from
[brultech.com](https://www.brultech.com/)), verified against live hardware.
Field offsets were additionally cross-checked against the community
`ecm2cloud.py` script that circulated on the CocoonTech and Brultech forums
around 2010–2012.

Cross-checking a published hardware specification is not the same as reusing
someone's code, and none was reused — but the people below worked out how to
talk to this hardware years before I did, and made that verification easy.
They deserve the credit.

## Prior work on the ECM-1240 protocol

The community effort around this meter goes back to roughly 2009 and passed
through a lot of hands. In rough order of appearance:

- **Amit Snyderman**
- **bpwwer** and **tenholde** — [CocoonTech](https://cocoontech.com/) forums
- **Kelvin Kakugawa**
- **Brian Jackson**
- **Marc MERLIN** — [marc.merlins.org](http://marc.merlins.org/), added net
  metering support and published a great deal of practical ECM-1240 material
- **Ben** — Brultech Research Inc.
- **Matthew Wall** — carried this hardware's software for over a decade

## Related projects

If this one does not suit you, these might — and they are worth knowing about
regardless:

- **[btmon.py](https://github.com/matthewwall/mtools)** — Matthew Wall, GPLv3.
  The best-established public ECM-1240 tool, and the direct descendant of the
  original `ecmread.py`. It supports the ECM-1220 and GEM as well, and uploads
  to a wide range of databases and services. **If you want a proven collector
  with many output targets, use btmon.** This project is a different shape: a
  self-contained local stack — collector, history, dashboard, insights — that
  keeps everything on one machine with no cloud service involved.
- **[ecmR library](https://github.com/pvanderwal/ecmR-library-for-Brultech-ECM-1240)** —
  Peter van der Wal, LGPL-2.1. A C library that parses ECM-1240 packets into
  shared memory for other applications to read.
- **[ecm1240-monitor-docker](https://github.com/tenstartups/ecm1240-monitor-docker)** —
  a Docker image for running btmon.py.

## Third-party libraries

| Library | Licence | Used for |
|---|---|---|
| [pyserial](https://github.com/pyserial/pyserial) | BSD-3-Clause | reading the meters over serial |
| [Flask](https://flask.palletsprojects.com/) | BSD-3-Clause | the read-only JSON API |
| [PyYAML](https://pyyaml.org/) | MIT | reading `config.yaml` |

Everything else is the Python standard library. The dashboard has **no
JavaScript dependencies at all** — no framework, no charting library; the
charts are hand-written SVG.

## Hardware documentation

The ECM-1240 manuals, packet format and command set are Brultech's copyrighted
documentation and are **not redistributed here**. Download them from
[brultech.com](https://www.brultech.com/). This project is not affiliated with
or endorsed by Brultech Research Inc.
