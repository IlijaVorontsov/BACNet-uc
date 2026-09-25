# Installing uc-hub on the site server

These steps turn a Linux PC on the building (or lab) network into the site
gateway: the hub runs as a systemd service, and a local mosquitto serves the
MQTT boards over mutual TLS. A TLS reverse proxy serves the web app to phones
and laptops. Written for Ubuntu 24.04 LTS; any systemd distribution with
Python 3.12 works the same way.

| Path | Contents |
|---|---|
| `/opt/uc-hub/BACNet-uc` | Deployment checkout of this repository (a tested commit, not your working copy) |
| `/etc/uc-hub/hub.yaml`, `site.yaml` | Configuration ([`hub.yaml`](hub.yaml), [`site.yaml`](site.yaml)) |
| `/etc/uc-hub/uc-hub.env` | Secrets ([`uc-hub.env.example`](uc-hub.env.example)) |
| `/etc/uc-hub/mqtt/` | The hub's MQTT client certificate |
| `/var/lib/uc-hub` | Database (manifest revisions, runs, approvals, audit). Back this up. |

## Network

- Wired Ethernet on the BMS/lab network, with a fixed address (DHCP
  reservation). Boards find the broker by this address or name.
- Inbound: TCP 8883 (MQTT over TLS, from boards) and 443 (the web app,
  through the proxy). The hub itself listens on 127.0.0.1:8080 only.
- UDP: the hub talks SMP to BACnet-uc boards (UDP 1337) and, when enabled,
  BACnet/IP (UDP 47808, including broadcasts on the local subnet). Allow these
  on the BMS interface.
- Outbound: HTTPS to `api.z.ai`.
- Windows PCs: use Linux. WSL2's default NAT networking drops the BACnet
  and SMP broadcasts; if WSL2 is unavoidable, use `networkingMode=mirrored`.

## 1. Packages and the checkout

```sh
sudo apt update
sudo apt install -y git curl openssl mosquitto mosquitto-clients python3.12 python3.12-venv
curl -LsSf https://astral.sh/uv/install.sh | sh              # uv
# Node 22 + pnpm, for building the web app
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs
sudo corepack enable

sudo useradd --system --home /var/lib/uc-hub --shell /usr/sbin/nologin uchub
sudo mkdir -p /opt/uc-hub && sudo chown "$USER" /opt/uc-hub
git clone -b claude/ai-harness-building-control-05nrgm https://github.com/IlijaVorontsov/BACNet-uc /opt/uc-hub/BACNet-uc
cd /opt/uc-hub/BACNet-uc/hub && uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e '.[dev]'
cd ../web && pnpm install --frozen-lockfile && pnpm build
```

Check that the checkout works before you configure anything:

```sh
cd /opt/uc-hub/BACNet-uc/hub && .venv/bin/pytest -q            # about 1.5 min
.venv/bin/uc-hub demo                                         # http://127.0.0.1:8080/, Ctrl-C to stop
ZAI_API_KEY=... .venv/bin/uc-hub demo --llm zai                # the same demo with GLM-5.3
```

The last command is the first run against the real model. Try "Commission
room 204" and an IO checkout, and compare with the scripted runs.

## 2. Z.ai key

uc-hub calls the standard API (`https://api.z.ai/api/paas/v4/chat/completions`,
model `glm-5.3`) and pays from the account balance. Create an API key in
the Z.ai console and top up the balance. At the time of writing GLM-5.3 costs
about $1.40 per million input tokens and $4.40 per million output tokens.
A **GLM Coding Plan** subscription is a different product: Z.ai limits its
quota to its supported coding tools and gives it its own endpoint, so do not
point uc-hub at the coding endpoint.

## 3. MQTT broker with mutual TLS

```sh
PKI=/root/uc-hub-pki          # the site CA; keep it off the web-facing parts
sudo /opt/uc-hub/BACNet-uc/deploy/mqtt-pki.sh init  $PKI <server-name> <server-ip>
sudo /opt/uc-hub/BACNet-uc/deploy/mqtt-pki.sh issue $PKI uc-hub
sudo /opt/uc-hub/BACNet-uc/deploy/mqtt-pki.sh issue $PKI <board-client-id>   # one per board

sudo install -d -m 755 /etc/mosquitto/certs
sudo install -m 644 $PKI/ca.crt $PKI/broker.crt /etc/mosquitto/certs/
sudo install -m 640 -g mosquitto $PKI/broker.key /etc/mosquitto/certs/
sudo install -m 644 /opt/uc-hub/BACNet-uc/deploy/mosquitto/uc-hub.conf /etc/mosquitto/conf.d/
sudo install -m 644 /opt/uc-hub/BACNet-uc/deploy/mosquitto/uc-hub.acl /etc/mosquitto/
sudo systemctl restart mosquitto
```

mosquitto drops to the `mosquitto` user before it reads its key, which is why
`broker.key` belongs to that group.

Boards: build `apps/mqtt_tls` (branch `claude/inter-session-communication-h989ye`)
with `CONFIG_APP_MQTT_BROKER_HOSTNAME` set to the server's name or address
(one of the names given to `init`), `CONFIG_APP_MQTT_TLS_CA_CERT_FILE` set to
`ca.crt`, and client authentication enabled with the board's certificate and
key (see that app's README). A board's client ID is `z` + base32 of its UID;
flash it once without mutual TLS, or read the ID from its boot log, then issue
its certificate.

## 4. Configure and start the hub

```sh
sudo install -d -m 750 -g uchub /etc/uc-hub /etc/uc-hub/mqtt
sudo install -m 640 -g uchub /opt/uc-hub/BACNet-uc/deploy/hub.yaml /opt/uc-hub/BACNet-uc/deploy/site.yaml /etc/uc-hub/
sudo install -m 644 $PKI/ca.crt $PKI/uc-hub.crt /etc/uc-hub/mqtt/
sudo install -m 640 -g uchub $PKI/uc-hub.key /etc/uc-hub/mqtt/
sudo install -m 640 -g uchub /opt/uc-hub/BACNet-uc/deploy/uc-hub.env.example /etc/uc-hub/uc-hub.env
sudoedit /etc/uc-hub/uc-hub.env           # ZAI_API_KEY and one token per person (openssl rand -hex 32)
sudoedit /etc/uc-hub/site.yaml            # the board's client ID; add devices
/opt/uc-hub/BACNet-uc/hub/.venv/bin/uc-hub validate /etc/uc-hub/site.yaml

sudo install -m 644 /opt/uc-hub/BACNet-uc/deploy/uc-hub.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now uc-hub
journalctl -u uc-hub -f
```

`site.yaml` is imported as revision 1 on the first start. From then on the
hub keeps its own revisions in `/var/lib/uc-hub`: change the site through the
agent (edit, plan, approve, apply), not by editing the file.

## 5. HTTPS for phones and laptops

Service workers (offline shell, installable app) and camera access need
HTTPS. Choose one:

- **Tailscale** (also gives remote access without opening ports):
  `sudo tailscale serve --bg --https=443 http://127.0.0.1:8080`, then open
  `https://<machine>.<tailnet>.ts.net/?token=<token>`.
- **Caddy** on the LAN: a site block with `reverse_proxy 127.0.0.1:8080` and
  `tls internal`, and install Caddy's root certificate on the phones.

Open `https://<hub>/?token=<token>` once per device. The app stores the
token and removes it from the address bar.

## Updating

```sh
cd /opt/uc-hub/BACNet-uc && git pull
(cd hub && uv pip install -p .venv/bin/python -e '.[dev]' && .venv/bin/pytest -q)
(cd web && pnpm install --frozen-lockfile && pnpm build)
sudo systemctl restart uc-hub             # releases every lease, then starts again
```

Back up `/var/lib/uc-hub` (the SQLite database; copy it while the service is
stopped, or use `sqlite3 uc-hub.db ".backup ..."`) and `/etc/uc-hub`.
