# Permanent install

Assumes Debian/Raspberry Pi OS. Adjust paths and the user name to taste.

## 1. Install

```bash
sudo apt install python3-pip python3-venv git sqlite3
sudo mkdir -p /opt/ecm1240 /var/lib/ecm1240 /etc/ecm1240
sudo chown "$USER" /opt/ecm1240 /var/lib/ecm1240 /etc/ecm1240

git clone https://github.com/YOUR-USERNAME/ecm1240-monitor.git /opt/ecm1240
cd /opt/ecm1240
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.yaml /etc/ecm1240/config.yaml
nano /etc/ecm1240/config.yaml
```

**Put the database on real storage, not the SD card.** At a five-second sample
rate an SD card will wear out — it is a question of when. A cheap USB SSD is
plenty. Set `database.path` to somewhere on that drive and make sure it is
mounted at boot via `/etc/fstab`.

## 2. Stable serial names (recommended)

`/dev/serial/by-path/` names are stable across reboots but ugly. If you prefer
friendly names, add a udev rule keyed on the USB path:

```bash
ls -l /dev/serial/by-path/            # note the path for each meter
sudo nano /etc/udev/rules.d/99-ecm1240.rules
```

```
SUBSYSTEM=="tty", KERNELS=="1-1.2:1.0", SYMLINK+="ecm-a"
SUBSYSTEM=="tty", KERNELS=="1-1.3:1.0", SYMLINK+="ecm-b"
```

```bash
sudo udevadm control --reload && sudo udevadm trigger
```

Key the rule on `KERNELS` (the physical port), **not** on vendor/product id —
two identical adapters cannot be told apart by id, which is the whole reason
identity comes from the port.

## 3. Services

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ecm1240-collector ecm1240-api
sudo systemctl enable --now ecm1240-insights.timer
```

Check it:

```bash
journalctl -fu ecm1240-collector
curl -s localhost:8080/api/health
```

The units assume the repo at `/opt/ecm1240`, the config at
`/etc/ecm1240/config.yaml`, and a user named `ecm`. Edit them if yours differ.

Create the user, or change `User=` to your own account:

```bash
sudo useradd -r -s /usr/sbin/nologin ecm
sudo usermod -aG dialout ecm          # dialout = permission to read serial ports
sudo chown -R ecm /var/lib/ecm1240
```

`dialout` group membership is the usual reason a service cannot open a port
that works fine when you run it by hand.

## 4. Optional: email digest

```bash
sudo tee /etc/ecm1240/env >/dev/null <<'EOF'
RESEND_API_KEY=re_your_key_here
EOF
sudo chmod 600 /etc/ecm1240/env
```

Then set `digest.enabled: true` in the config. The key is read from the
environment and must never be placed in `config.yaml`.

## 5. Backups

The database is the only irreplaceable part. A nightly snapshot:

```bash
sudo tee /etc/cron.d/ecm1240-backup >/dev/null <<'EOF'
17 3 * * * ecm sqlite3 /var/lib/ecm1240/energy.db ".backup '/var/lib/ecm1240/energy-backup.db'"
EOF
```

Use `.backup`, not `cp` — copying a file that is being written produces a
corrupt snapshot. Then rsync the backup somewhere else.

**Remember the backup is as sensitive as the original.** It contains the same
occupancy record. See [`SECURITY.md`](SECURITY.md).

## 6. Verify

```bash
python3 tools/verify_unit_identity.py --config /etc/ecm1240/config.yaml
```

Run this after any change to your USB wiring, and after any reboot that might
have renamed a port.
