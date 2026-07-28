# Security and privacy

## The short version

Your energy database is more sensitive than it looks. **Do not expose this to
the internet.** The defaults are safe; the ways people usually break them are
listed below.

## Why the data matters

A whole-house total tells someone roughly how much electricity you use. A
**per-circuit record sampled every five seconds tells them how you live**:

- when people get up and go to bed
- when the house is empty, and for how long
- when you went on holiday
- when someone is home alone during the day
- when you started using a new appliance

Security research on smart-meter data has shown for years that appliance-level
consumption reveals occupancy reliably. Your data is finer-grained than a
utility meter's. Treat the database the way you would treat a door-entry log.

## What this software exposes

| Component | Default | Authentication |
|---|---|---|
| Read-only API (`api.bind`) | `127.0.0.1:8080` | **None** |
| Live CSV feed (`collector.live_feed_port`) | disabled | **None** |
| Gateway listener (`--gateway`) | off unless requested | **None** |

There is no login anywhere in this project. That is a deliberate, stated
limitation, not an oversight — but it means **network placement is your only
access control.**

## Rules

### 1. Do not port-forward the API

If you want to see your dashboard from outside the house, use a VPN
(WireGuard or Tailscale are both easy). Forwarding port 8080 puts an
unauthenticated occupancy feed on the public internet, where it will be found
by automated scanners within days.

### 2. Think before setting `api.bind: 0.0.0.0`

That makes the API readable by **every device on your network** — guest phones,
a smart TV, an IoT gadget with poor firmware. On a home LAN that may be an
acceptable trade so you can view the dashboard from a tablet. Make it a
decision, not an accident.

### 3. Leave `allowed_origin` blank

Setting it to `"*"` lets **any website anyone in your house visits** read your
entire energy history through their browser and send it anywhere. This is the
one setting here with a genuine remote-exploitation path. If you need
cross-origin access, name the specific origin.

### 4. Keep the API key out of the config file

The optional email digest needs a Resend API key. It is read from the
`RESEND_API_KEY` environment variable, never from `config.yaml`. Put it in a
systemd `EnvironmentFile` with mode `600`.

### 5. Never commit `config.yaml`, `profiles.yaml` or the database

They are all in `.gitignore`. `config.yaml` holds your circuit names and email;
the database holds everything above.

### 6. Be careful what you publish

Screenshots of a real dashboard show your real circuits and your real daily
pattern. If you want to share the project or ask for help on a forum, use the
demo data:

```bash
python3 tools/make_demo_data.py --db demo.db --days 7
python3 -m ecm1240.api --config config.demo.yaml
```

Before posting a database extract or a log, check it does not contain a week of
your occupancy.

## Known limitations

These are stated plainly rather than hidden:

- **No authentication on any listener.** Placement is the only control.
- **The event/gateway listeners accept data from anyone who can reach them.**
  Nothing verifies the sender. On a trusted LAN this is fine; anywhere else it
  allows fabricated readings to be injected into your database.
- **SQLite has no access control.** Anyone who can read the file has everything.
- **Backups inherit all of the above.** If you rsync the database to a NAS, that
  copy is just as sensitive.

## Reporting a vulnerability

Open an issue for anything non-sensitive. For something you would rather not
post publicly, open an issue asking for a contact address without including the
detail.
