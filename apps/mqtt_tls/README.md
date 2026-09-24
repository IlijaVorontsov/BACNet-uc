# MQTT over Ethernet + TLS (Zephyr)

An MQTT 3.1.1 client for wired Ethernet boards on **Zephyr 4.4.2**. It is the
MQTT counterpart of the BACnet firmware in this repository, and it uses the
same Zephyr version, boards and west workspace.

| Board | MCU | Ethernet | Entropy for TLS | Watchdog |
|---|---|---|---|---|
| `nucleo_f767zi` | STM32F767 (Cortex-M7) | on-chip MAC + LAN8742A | STM32 RNG | IWDG |
| `frdm_mcxn947/mcxn947/cpu0` | MCX N947 (Cortex-M33) | ENET QoS + PHY | ELS TRNG | WWDT |
| `nucleo_h563zi` | STM32H563 (Cortex-M33) | on-chip MAC + LAN8742A | STM32 RNG | IWDG |
| `native_sim/native/64` | host (tests) | host sockets (NSOS) | host | – |

The client connects to a broker over **TLS 1.3 or 1.2** (Mbed TLS 4) and
verifies the broker's certificate. It can also authenticate itself with a
client certificate (mutual TLS). It then:

- publishes a retained `online` status once its command subscription is
  confirmed, and sets a retained `offline` last will;
- publishes a retained info record that describes the device and its
  capabilities;
- publishes telemetry periodically (QoS 1 by default);
- executes commands (plain text or JSON with a request ID) and answers on its
  event topic;
- detects a dead connection and reconnects with exponential back-off and jitter.
  Every blocking call is bounded, and a watchdog backs this up.

## Topics

`<root>` is `CONFIG_APP_MQTT_TOPIC_ROOT` (default `bacnet-uc`). `<id>` is the
client ID. If `CONFIG_APP_MQTT_CLIENT_ID` is empty, the ID is `z` followed by 20
base32 characters derived from the MCU's unique ID. The 96-bit STM32 UID is used
as is; the 128-bit MCX N UID is condensed to 96 bits with SHA-256. Either way
the ID stays within the 23 alphanumeric characters every MQTT 3.1.1 broker must
accept.

| Topic | Direction | Retained | Payload |
|---|---|---|---|
| `<root>/<id>/status` | device → broker | yes | `online`, or the last will `offline` |
| `<root>/<id>/info` | device → broker | yes | see below |
| `<root>/<id>/telemetry` | device → broker | no | `{"seq":N,"uptime_s":S,"sessions":K}` |
| `<root>/<id>/cmd` | broker → device | – | a command (see below) |
| `<root>/<id>/event` | device → broker | no | the reply to a command |

The info record:

```json
{"fw":"0.3.0","board":"nucleo_f767zi","zephyr":"4.4.2",
 "hwid":"<unique ID in hex>","mac":"00:80:e1:..","ip":"192.168.1.20","tls":true,
 "caps":{"cmds":["ping","led","identify"],
         "telemetry":{"seq":"count","uptime_s":"s","sessions":"count"}}}
```

`caps.cmds` lists `led` and `identify` only when the board has an LED. The
uc-hub gateway builds its point list from `caps`.

### Commands

| Plain text | JSON | Reply |
|---|---|---|
| `ping` | `{"cmd":"ping"}` | `{"ok":true,"pong":<uptime s>}` |
| `led on`, `led off`, `led toggle` | `{"cmd":"led","arg":"on"}` | `{"ok":true,"led":true}` |
| `identify [seconds]` | `{"cmd":"identify","arg":"30"}` | `{"ok":true,"identify":30}` |

- `identify` blinks the user LED (`led0`), for 30 s by default, so someone in
  the field can find the board. `identify 0` stops it, and so does any `led`
  command.
- A JSON request may carry an `"id"` of up to 16 characters from
  `[A-Za-z0-9._:-]`. The reply echoes it, e.g.
  `{"id":"r1","ok":true,"led":true}`, so a client can match replies when several
  clients send commands.
- Errors look like `{"ok":false,"error":"unknown command"}`. The possible errors
  are `bad argument`, `led unavailable`, `invalid json`, `invalid id`,
  `missing cmd` and `payload too large`. The `id` is echoed here too when it is
  valid.
- Publish commands **without** the retain flag. The device ignores retained
  commands, because the broker would replay them after every reconnect. It also
  ignores empty messages, which are what clearing a retained message produces.

## Build

The west workspace is described in the repository's [`west.yml`](../../west.yml)
(shared with the BACnet firmware). [`docs/SESSION_NOTES.md`](../../docs/SESSION_NOTES.md)
covers installing Zephyr SDK 1.0.1 and Python 3.12 in a fresh container. From
the directory that contains this repository:

```sh
west init -l BACNet-uc && west update --narrow -o=--depth=1
west build -b nucleo_f767zi BACNet-uc/apps/mqtt_tls        # or frdm_mcxn947/mcxn947/cpu0
west flash
```

With the defaults, the device connects to the public test broker
`test.mosquitto.org:8883` and trusts the CA that is committed as
`certs/mosquitto.org.crt`. Watch it with:

```sh
mosquitto_sub -h test.mosquitto.org -p 8883 --cafile apps/mqtt_tls/certs/mosquitto.org.crt \
  -t 'bacnet-uc/#' -v
mosquitto_pub -h test.mosquitto.org -p 8883 --cafile apps/mqtt_tls/certs/mosquitto.org.crt \
  -t 'bacnet-uc/<id>/cmd' -m 'identify 10'
```

Anyone can read and write on the public test broker. Use your own broker for
anything real.

### Configuring your own broker

Put the options in a file, e.g. `my-broker.conf`, and pass it with
`-- -DEXTRA_CONF_FILE=my-broker.conf`:

```ini
CONFIG_APP_MQTT_BROKER_HOSTNAME="mqtt.example.com"
CONFIG_APP_MQTT_BROKER_PORT=8883
CONFIG_APP_MQTT_TLS_CA_CERT_FILE="/path/to/ca.pem"

# mutual TLS
CONFIG_APP_MQTT_TLS_CLIENT_AUTH=y
CONFIG_APP_MQTT_TLS_CLIENT_CERT_FILE="/path/to/device.crt"
CONFIG_APP_MQTT_TLS_CLIENT_KEY_FILE="/path/to/device.key"

# or user name / password
CONFIG_APP_MQTT_USERNAME="device-1"
CONFIG_APP_MQTT_PASSWORD="secret"
```

Relative credential paths are resolved against `apps/mqtt_tls/`. Files ending in
`.der` are embedded as DER; anything else is treated as PEM. The credentials
are parsed at boot. A certificate or key that Mbed TLS cannot use is reported
there with its error code. The device does not retry with it. Private keys
must be unencrypted.

The broker's host name is sent as SNI and matched against its certificate. If
you address the broker by IP address, set `CONFIG_APP_MQTT_TLS_HOSTNAME` to the
name in its certificate. Otherwise the IP literal is sent as SNI, which
multi-tenant cloud brokers reject.

Brokers that speak **only TLS 1.2 and use an RSA key** need
`-DEXTRA_CONF_FILE=overlay-tls12-rsa.conf` (you can list it together with your
broker file, separated by `;`). The default build supports TLS 1.3, which
requires RSA-PSS for RSA certificates. With PSS enabled, Mbed TLS 4.1's TLS 1.2
client rejects the PSS signature such a broker picks. ECDSA brokers, and RSA
brokers that support TLS 1.3, work with the default build.

SHA-1 is not built in, so CA roots that are self-signed with SHA-1 cannot be
loaded. "DigiCert Global Root CA" is one example. Use a SHA-256 root, such as
DigiCert Global Root G2, ISRG Root X1 or Amazon Root CA 1, or use the
SHA-256-signed intermediate as the trust anchor.

`scripts/gen_dev_certs.sh [out_dir] [broker names...]` creates a throw-away CA
with a broker certificate and a device certificate, for a local broker (ECDSA P-256 keys, or
RSA-2048 with `KEY_TYPE=rsa`). It
writes to `certs/dev/`, which is git-ignored, and the default client
certificate and key paths point there. `*.key` files are git-ignored
everywhere in the app. The build directory contains the embedded key
(`zephyr/include/generated/app_creds/`), so treat it as secret too.

For debugging against a local broker without TLS, `-DFILE_SUFFIX=plain` builds
with `prj_plain.conf`. That variant uses port 1883 and leaves out Mbed TLS
entirely.

All options are listed in [`Kconfig`](Kconfig) (`CONFIG_APP_*`).

## Test on the host (no hardware)

`native_sim/native/64` builds the same application for Linux. With Zephyr's
offloaded sockets (NSOS), Zephyr socket calls go to the host's sockets. No TAP
interface or root is needed, and Mbed TLS still runs inside Zephyr. The
end-to-end test starts a local mosquitto that requires mutual TLS and checks
the application against it:

```sh
sudo apt-get install mosquitto mosquitto-clients   # once
apps/mqtt_tls/scripts/e2e_native_sim.sh
```

The test covers:

- mutual TLS: broker verification by CA and host name, and device
  authentication by certificate;
- retained status and info (with `fw`, `hwid` and `caps`), and periodic
  telemetry;
- plain-text and JSON commands, request ID echo, `identify`, and each error
  reply;
- an oversized payload that must be drained with the session kept;
- detection of a frozen broker by PINGREQ/PINGRESP timeout, and reconnect;
- retained and empty commands being ignored;
- a broker that stalls halfway through a 128 MB payload: the bounded socket read
  ends the session instead of wedging the device;
- growing back-off across a broker restart;
- the last will after the broker's keep-alive drops a frozen device;
- rejection of an unknown CA, of a wrong host name, and of a device without a
  certificate;
- acceptance of an IP-address SAN;
- SNI, and complete TLS 1.2 and TLS 1.3 handshakes;
- RSA: a PKCS#8 RSA client key over mutual TLS, an RSA broker certificate over
  TLS 1.3 (RSA-PSS), and a TLS-1.2-only RSA broker with the overlay.

Two things are not covered on the host: loss of the network interface (the
offloaded-socket target has no Zephyr-managed interface) and the hardware
watchdog. Both need the board.

## Resource use (Zephyr 4.4.2, SDK 1.0.1)

| Board | Flash | RAM |
|---|---|---|
| `nucleo_f767zi` | 247 KB | 142 KB of 384 KB |
| `frdm_mcxn947/mcxn947/cpu0` | 249 KB | 147 KB of 320 KB |
| `nucleo_h563zi` | 250 KB | 154 KB of 256 KB |
| `frdm_mcxn947/mcxn947/cpu0`, plain (`FILE_SUFFIX=plain`) | 135 KB | 64 KB |

The figures include a 64 KB Mbed TLS heap, sized for full 16 KB TLS records,
and an 8 KB main stack for the TLS handshake. They also include 96 network RX
buffers (a 4 KB TCP window) and larger network thread stacks. `prj.conf`
explains each choice.

## Robustness

- **Bounded blocking.** The TCP connect (`CONFIG_NET_SOCKETS_CONNECT_TIMEOUT`)
  and the TLS handshake (`CONFIG_NET_SOCKETS_TLS_CONNECT_TIMEOUT`) are each
  limited to 15 s. The handshake limit includes the software ECC and
  certificate checks. Every socket read
  and write in a session is limited by `SO_RCVTIMEO`/`SO_SNDTIMEO`. Zephyr's TLS
  sockets otherwise wait forever.
- **Dead-peer detection.** If nothing has been received for a keep-alive
  period, the device sends PINGREQ. If no PINGRESP arrives within
  min(keep-alive, 30 s), the device drops the session.
- **Subscription check.** A SUBACK that is rejected, for example by a broker ACL,
  or that never arrives ends the session and is retried with back-off. The
  device never announces `online` while it cannot receive commands.
- **Watchdog.** A task-watchdog channel, backed by the hardware watchdog,
  reboots the board if the main loop makes no progress for 120 s
  (`CONFIG_APP_WATCHDOG_TIMEOUT_SEC`). It is armed only after the configuration
  and credentials have been checked, so a misconfigured device logs its error
  instead of reboot-looping. The hardware window is 2 s, fed every second,
  which tolerates the watchdog oscillator's inaccuracy.

## Board notes

- **NUCLEO-F767ZI:** `boards/nucleo_f767zi.conf` enables the Cortex-M7 caches
  (`CONFIG_CACHE_MANAGEMENT`), which Zephyr 4.4 leaves off on F7. Without them
  the TLS handshake runs from uncached flash. The Ethernet DMA buffers live in
  DTCM, so caching is safe. The overlay disables SPI1, whose MOSI pin (PA7) is
  the RMII CRS_DV line. The MAC address is derived from the UID and stays the
  same for a given chip.
- **FRDM-MCXN947:** the overlay replaces the board's random MAC, which would be
  new on every boot and cause a new DHCP lease after each watchdog reset, with
  the stable UID-based MAC (`nxp,unique-mac`). The ENET QoS RX ring is enlarged
  to 48 descriptors (`CONFIG_NET_BUF_RX_COUNT=128`), because the driver
  permanently reserves one network buffer per descriptor.
- Both boards' Ethernet drivers follow the PHY's negotiated speed and duplex.

## Security notes

- The broker certificate is verified by default: the CA and the host name are
  checked (`CONFIG_APP_MQTT_TLS_PEER_VERIFY`). Certificate validity dates are
  **not** checked, because the board has no trusted time source at boot. To
  add expiry and not-yet-valid checks, enable `CONFIG_MBEDTLS_HAVE_TIME_DATE`.
  You must then also set `CLOCK_REALTIME` before the first handshake, for
  example with SNTP and `clock_settime()`. Otherwise the clock reads 1970 and
  every certificate is rejected as not yet valid.
- Only forward-secret key exchanges with AES-GCM are offered: TLS 1.3 with
  AES-128/256-GCM, and TLS 1.2 with ECDHE-ECDSA or ECDHE-RSA and AES-GCM.
  Supported curves are X25519, P-256 and P-384.
- Client keys and passwords are embedded in the firmware image. On production
  parts, enable flash read-out protection (STM32 RDP, MCX N debug/flash
  protection), or keep the key in a secure element.
- Each board's hardware TRNG feeds Mbed TLS. Do not enable
  `CONFIG_TEST_RANDOM_GENERATOR`: some upstream samples do, and it makes TLS
  keys predictable.
