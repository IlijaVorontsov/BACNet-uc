# MQTT over Ethernet + TLS (Zephyr, STM32)

An MQTT 3.1.1 client for the **NUCLEO-H563ZI** (STM32H563, Cortex-M33, on-chip
10/100 Ethernet) on **Zephyr 3.7 LTS**. It connects to a broker over **TLS 1.2**
(mbedTLS) and verifies the broker's certificate. It can also authenticate
itself with a client certificate (mutual TLS). It then:

- publishes a retained `online` status once its command subscription is
  confirmed, and sets a retained `offline` last will;
- publishes a retained info record (board, Zephyr version, IP address);
- publishes telemetry periodically (QoS 1 by default);
- executes commands received on its command topic and answers on its event
  topic;
- detects a dead connection and reconnects with exponential back-off and jitter.
  Every blocking call is bounded, and a watchdog backs this up.

It is the MQTT counterpart of the BACnet application that is being developed
alongside it in this repository, and uses the same Zephyr version, board and
west workspace.

## Topics

`<root>` is `CONFIG_APP_MQTT_TOPIC_ROOT` (default `bacnet-uc`). `<id>` is the
client ID: `CONFIG_APP_MQTT_CLIENT_ID`, or, when that option is empty, `z` followed
by the MCU's 96-bit unique ID in 20 base32 characters. That keeps the ID within
the 23 alphanumeric characters every MQTT 3.1.1 broker must accept.

| Topic | Direction | Retained | Payload |
|---|---|---|---|
| `<root>/<id>/status` | device → broker | yes | `online`, or the last will `offline` |
| `<root>/<id>/info` | device → broker | yes | `{"board":…,"zephyr":…,"ip":…,"tls":true}` |
| `<root>/<id>/telemetry` | device → broker | no | `{"seq":N,"uptime_s":S,"sessions":K}` |
| `<root>/<id>/cmd` | broker → device | – | `ping`, `led on`, `led off`, `led toggle` |
| `<root>/<id>/event` | device → broker | no | Command replies, e.g. `{"pong":S}`, `{"led":true}`, `{"error":…}` |

Publish commands **without** the retain flag. The device ignores retained
commands, because the broker would replay them after every reconnect. It also
ignores empty messages, which are what clearing a retained message produces.

## Build

The west workspace is described in the repository's [`west.yml`](../../west.yml)
and in [`docs/SESSION_NOTES.md`](../../docs/SESSION_NOTES.md), which covers
installing Zephyr SDK 0.16.8 and the modules in a fresh container. From the
directory that contains this repository:

```sh
west init -l BACNet-uc && west update --narrow -o=--depth=1
west build -b nucleo_h563zi BACNet-uc/apps/mqtt_tls
west flash
```

With the defaults, the device connects to the public test broker
`test.mosquitto.org:8883` and trusts the CA that is committed as
`certs/mosquitto.org.crt`. Watch it with:

```sh
mosquitto_sub -h test.mosquitto.org -p 8883 --cafile apps/mqtt_tls/certs/mosquitto.org.crt \
  -t 'bacnet-uc/#' -v
mosquitto_pub -h test.mosquitto.org -p 8883 --cafile apps/mqtt_tls/certs/mosquitto.org.crt \
  -t 'bacnet-uc/<id>/cmd' -m 'led toggle'
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
are parsed at boot. A certificate or key that mbedTLS cannot use is reported
there with its error code. The device does not retry with it. Private keys
must be unencrypted.

The broker's host name is sent as SNI and matched against its certificate. If
you address the broker by IP address, set `CONFIG_APP_MQTT_TLS_HOSTNAME` to the
name in its certificate. Otherwise the IP literal is sent as SNI, which
multi-tenant cloud brokers reject.

SHA-1 is not built in, so CA roots that are self-signed with SHA-1 cannot be
loaded. "DigiCert Global Root CA" is one example. Use a SHA-256 root, such as
DigiCert Global Root G2, ISRG Root X1 or Amazon Root CA 1, or use the
SHA-256-signed intermediate as the trust anchor.

`scripts/gen_dev_certs.sh [out_dir] [broker names...]` creates a throw-away CA
with a broker certificate and a device certificate, for a local broker. It
writes to `certs/dev/`, which is git-ignored, and the default client
certificate and key paths point there. `*.key` files are git-ignored
everywhere in the app. The build directory contains the embedded key
(`zephyr/include/generated/app_creds/`), so treat it as secret too.

For debugging against a local broker without TLS, `-DFILE_SUFFIX=plain` builds
with `prj_plain.conf`. That variant uses port 1883 and leaves out mbedTLS
entirely.

All options are listed in [`Kconfig`](Kconfig) (`CONFIG_APP_*`).

## Test on the host (no hardware)

`native_sim/native/64` builds the same application for Linux. With Zephyr's
offloaded sockets (NSOS), Zephyr socket calls go to the host's sockets. No TAP
interface or root is needed, and mbedTLS still runs inside Zephyr. The
end-to-end test starts a local mosquitto that requires mutual TLS and checks
the application against it:

```sh
sudo apt-get install mosquitto mosquitto-clients   # once
apps/mqtt_tls/scripts/e2e_native_sim.sh
```

The test covers:

- mutual TLS: broker verification by CA and host name, and device
  authentication by certificate;
- retained status and info, and periodic telemetry;
- command replies, including an oversized payload that must be drained with
  the session kept;
- detection of a frozen broker by PINGREQ/PINGRESP timeout, and reconnect;
- retained and empty commands being ignored;
- a broker that stalls halfway through a 128 MB payload: the bounded socket read
  ends the session instead of wedging the device;
- growing back-off across a broker restart;
- the last will after the broker's keep-alive drops a frozen device;
- rejection of an unknown CA, of a wrong host name, and of a device without a
  certificate;
- acceptance of an IP-address SAN;
- SNI being present in the ClientHello.

Two things are not covered on the host: loss of the network interface (the
offloaded-socket target has no Zephyr-managed interface) and the hardware
watchdog. Both need the board.

## Resource use (nucleo_h563zi, Zephyr 3.7.2, SDK 0.16.8)

| | Flash | RAM (SRAM1, 256 KB) |
|---|---|---|
| MQTT + TLS (default) | 212 KB | 150 KB |
| MQTT + mutual TLS | 215 KB | 150 KB |
| Plain MQTT (`FILE_SUFFIX=plain`) | 122 KB | 70 KB |

The TLS figures include a 64 KB mbedTLS heap, sized for full 16 KB TLS records,
and an 8 KB main stack for the TLS handshake. They also include 96 network RX
buffers (a 4 KB TCP window) and larger network thread stacks. `prj.conf`
explains each choice.

## Robustness

- **Bounded blocking.** The TCP connect and the TLS handshake are each limited
  to 15 s (`CONFIG_NET_SOCKETS_CONNECT_TIMEOUT`). That limit includes the
  software ECC and certificate checks, not just the SYN-ACK. Every socket read
  and write in a session is limited by `SO_RCVTIMEO`/`SO_SNDTIMEO`. Zephyr's TLS
  sockets otherwise wait forever.
- **Dead-peer detection.** If nothing has been received for a keep-alive
  period, the device sends PINGREQ. If no PINGRESP arrives within
  min(keep-alive, 30 s), the device drops the session.
- **Subscription check.** A SUBACK that is rejected, for example by a broker ACL,
  or that never arrives ends the session and is retried with back-off. The
  device never announces `online` while it cannot receive commands.
- **Watchdog.** A task-watchdog channel, backed by the STM32 IWDG, reboots the
  board if the main loop makes no progress for 120 s
  (`CONFIG_APP_WATCHDOG_TIMEOUT_SEC`). It is armed only after the configuration
  and credentials have been checked, so a misconfigured device logs its error
  instead of reboot-looping.
- **Ethernet speed and duplex.** In Zephyr 3.7, the STM32H5 Ethernet driver
  runs the MAC at 100 Mbit/s full duplex whatever the PHY negotiates. The app
  therefore limits the PHY advertisement to 100BASE-TX full duplex
  (`CONFIG_APP_ETH_PHY_ADVERTISE_100FD_ONLY`). A 10 Mbit/s or half-duplex
  switch port then shows no link at all, instead of a link that silently drops
  frames. Connect the board to a 100 Mbit/s full-duplex port with
  autonegotiation.

## Security notes

- The broker certificate is verified by default: the CA and the host name are
  checked (`CONFIG_APP_MQTT_TLS_PEER_VERIFY`). Certificate validity dates are
  **not** checked, because the board has no trusted time source at boot. To
  add expiry and not-yet-valid checks, enable `CONFIG_MBEDTLS_HAVE_TIME_DATE`.
  You must then also set `CLOCK_REALTIME` before the first handshake, for
  example with SNTP and `clock_settime()`. Otherwise the clock reads 1970 and
  every certificate is rejected as not yet valid.
- Only forward-secret ECDHE key exchanges with AES-GCM are offered (TLS 1.2).
- Client keys and passwords are embedded in the firmware image. On production
  parts, enable STM32 read-out protection, or move the key into a secure
  element or the STM32H573's secure storage.
- The STM32 hardware RNG feeds mbedTLS. Do not enable
  `CONFIG_TEST_RANDOM_GENERATOR`: some upstream samples do, and it makes TLS
  keys predictable on boards without an entropy driver.
