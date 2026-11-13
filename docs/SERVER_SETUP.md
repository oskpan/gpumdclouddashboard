# Hub server setup (gpumdq_hub.py)

The hub is one Python file (standard library only) that shows every PC running gpumdq on one HTTPS page.
The PCs push to it; it never connects to them. These steps were used on an Alibaba Cloud Linux 3 ECS instance
and also work on Ubuntu/Debian. Commands are run as root; prefix them with `sudo` otherwise.

Requirements: Linux server with a public IP, Python ≥ 3.8, ports 80 and 443 reachable from the internet.
A domain name is optional (Let's Encrypt issues certificates for bare IP addresses).

## 1. Open the firewall
Cloud security group (e.g. Alibaba ECS → Security Groups → Inbound): allow TCP 80 and 443 from 0.0.0.0/0.
Do **not** open 8780; the hub listens on 127.0.0.1 only and Caddy is the public entry point.
If the server also runs ufw/firewalld, open 80/443 there too.

## 2. Copy the files
From the PC (PowerShell or WSL):
```bash
scp gpumdq.py gpumdq_hub.py deploy/Caddyfile deploy/gpumdq-hub.service root@SERVER_IP:/tmp/
```
`gpumdq.py` must sit next to `gpumdq_hub.py`: the hub reuses its dashboard for the per-PC pages.

## 3. Install the hub, create the login and one token per PC
```bash
useradd --system --home /var/lib/gpumdq-hub --create-home --shell /usr/sbin/nologin gpumdq
mkdir -p /opt/gpumdq && cp /tmp/gpumdq.py /tmp/gpumdq_hub.py /opt/gpumdq/
H="runuser -u gpumdq -- env GPUMDQ_HUB_HOME=/var/lib/gpumdq-hub python3 /opt/gpumdq/gpumdq_hub.py"
$H set-password <you>      # web login (≥ 10 characters), stored as a PBKDF2 hash
$H add-host PC1            # prints PC1's token once; only its SHA-256 is stored. Repeat per PC.
$H list
```
Run these commands **as the gpumdq user** (as above). If `hub.json` ends up owned by root, the service cannot
read it, assumes no login exists and exits in a restart loop. Fix:
`chown -R gpumdq:gpumdq /var/lib/gpumdq-hub && chmod 600 /var/lib/gpumdq-hub/hub.json`.

Tokens and logins are re-read on every request: `add-host`, `remove-host` and `set-password` need no restart.
Re-running `add-host NAME` replaces that PC's token.

## 4. Run it as a service
```bash
cp /tmp/gpumdq-hub.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now gpumdq-hub
systemctl status gpumdq-hub --no-pager | head -5
curl http://127.0.0.1:8780/healthz           # {"ok": true}
```
| `systemctl status` shows | cause / fix |
|---|---|
| `status=203/EXEC` | python3 is not at /usr/bin/python3: `sed -i "s#/usr/bin/python3#$(which python3)#" /etc/systemd/system/gpumdq-hub.service`, then daemon-reload + restart |
| `status=226/NAMESPACE` | /var/lib/gpumdq-hub missing: create it, chown to gpumdq |
| restarts every 5 s, log says "no viewer login yet" | see the ownership note in step 3 |

## 5. HTTPS with Caddy (v2.11 or newer)
Older Caddy packages (e.g. Ubuntu's own `caddy` 2.6) cannot request IP certificates. Install from Caddy:

Ubuntu / Debian:
```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```
Alibaba Cloud Linux / CentOS / RHEL / other (official static binary):
```bash
curl -L -o /usr/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=amd64" && chmod +x /usr/bin/caddy
groupadd --system caddy; useradd --system --gid caddy --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
mkdir -p /etc/caddy
curl -L -o /etc/systemd/system/caddy.service https://raw.githubusercontent.com/caddyserver/dist/master/init/caddy.service
systemctl daemon-reload && systemctl enable caddy
getenforce    # if "Enforcing": setenforce 0 and set SELINUX=permissive in /etc/selinux/config
```
Configure and start:
```bash
cp /tmp/Caddyfile /etc/caddy/Caddyfile
sed -i 's/203.0.113.10/SERVER_IP/g' /etc/caddy/Caddyfile     # your public IP, in both places
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl restart caddy
sleep 25; journalctl -u caddy --since "2 min ago" --no-pager | grep -iE "certificate obtained|error" | tail -5
curl https://SERVER_IP/healthz                                  # {"ok": true}
```
Notes from the first deployment:
- **`tlsv1 alert internal error`** although the log says "certificate obtained successfully": the
  `default_sni` global option is missing. Clients send no SNI to an IP address, so Caddy must be told which
  certificate to present. It is in `deploy/Caddyfile`.
- **"Domain name does not end with a valid public suffix"**: the Caddyfile still contains a placeholder name.
- IP certificates are valid for ~6 days; Caddy renews them automatically every few days. Keep port 80 open.
- With a domain name, use Option B in `deploy/Caddyfile` instead (normal 90-day certificate, no `default_sni`).

Open `https://SERVER_IP` in a browser: it asks for the login from step 3 and shows "No PC has reported yet".

## 6. Optional: phone alerts
Install the ntfy app and subscribe to a long random topic. Add to the service file
`Environment=GPUMDQ_NTFY=https://ntfy.sh/<topic>`, then `systemctl daemon-reload && systemctl restart gpumdq-hub`.
Alerts: queued job done/failed, batch script ended (with the number of incomplete stages),
PC silent for > 6 push intervals (min 60 s), and PC reporting again.

## 7. Hardening
- SSH: key login only (`PasswordAuthentication no` in /etc/ssh/sshd_config, restart `sshd`/`ssh`).
  Public cloud IP ranges see constant SSH brute-force attempts.
- Keep the hub bound to 127.0.0.1 (the default). `--no-auth` is refused on any other address.
- The service unit runs as an unprivileged user with `ProtectSystem=strict`; only /var/lib/gpumdq-hub is writable.

## 8. Maintenance
| task | command |
|---|---|
| hub log | `journalctl -u gpumdq-hub -f` |
| Caddy log | `journalctl -u caddy -f` |
| update the code | copy new `gpumdq.py` + `gpumdq_hub.py` to /opt/gpumdq, `systemctl restart gpumdq-hub` (PCs reconnect by themselves) |
| remove a PC | `$H remove-host PC3` |
| state files | `/var/lib/gpumdq-hub/hub.json` (hashed tokens + logins), `hub_state.json` (last snapshot per PC) |

## Fallback without public HTTPS: SSH tunnel
If ports 80/443 cannot be opened (e.g. a mainland-China server without ICP filing), skip Caddy.
On each PC (WSL, with an SSH key for the server):
```bash
sudo apt install -y autossh
autossh -M 0 -f -N -o ServerAliveInterval=30 -L 8780:127.0.0.1:8780 user@SERVER_IP
```
and set `GPUMDQ_HUB=http://127.0.0.1:8780` in that PC's `hub.env`. View the page through the same tunnel at
http://localhost:8780.
