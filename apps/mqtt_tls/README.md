# MQTT over Ethernet + TLS (Zephyr, STM32)

An MQTT 3.1.1 client for the **NUCLEO-H563ZI** (STM32H563, Cortex-M33, on-chip
10/100 Ethernet) on **Zephyr 3.7 LTS**. It connects to a broker over **TLS 1.2**
(mbedTLS) and verifies the broker's certificate. It can also authenticate
itself with a client certificate (mutual TLS). It then:

- publishes a retained `online` status and sets a retained `offline` last will;
- publishes a retained info record (board, Zephyr version, IP address);
- publishes telemetry periodically (QoS 1 by default);
- executes commands received on its command topic and answers on its event
  topic;
- detects a dead connection and reconnects with exponential back-off and jitter.

It is the MQTT counterpart of the BACnet application that is being developed
alongside it in this repository, and uses the same Zephyr version, board and
west workspace.

## Topics

`<root>` is `CONFIG_APP_MQTT_TOPIC_ROOT` (default `bacnet-uc`). `<id>` is the
client ID: `CONFIG_APP_MQTT_CLIENT_ID`, or `zephyr-<unique MCU id in hex>` when
that option is empty.

| Topic | Direction | Retained | Payload |
|---|---|---|---|
| `<root>/<id>/status` | device → broker | yes | `online`, or the last will `offline` |
| `<root>/<id>/info` | device → broker | yes | `{"board":…,"zephyr":…,"ip":…,"tls":true}` |
| `<root>/<id>/telemetry` | device → broker | no | `{"seq":N,"uptime_s":S,"sessions":K}` |
| `<root>/<id>/cmd` | broker → device | – | `ping`, `led on`, `led off`, `led toggle` |
| `<root>/<id>/event` | device → broker | no | Command replies, e.g. `{"pong":S}`, `{"led":true}`, `{"error":…}` |

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
`.der` are embedded as DER; anything else is treated as PEM. If the broker is
addressed by IP address but its certificate names a host, set
`CONFIG_APP_MQTT_TLS_HOSTNAME` to that host name. The name is used for SNI and
certificate verification.

`scripts/gen_dev_certs.sh [out_dir] [broker names...]` creates a throw-away CA
with a broker certificate and a device certificate, for a local broker.

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

The test checks the TLS handshake with broker verification and a client
certificate, the status, info and telemetry messages, command replies
(including an oversized payload that must be drained), reconnect after a broker
restart, and the last will. It also checks the negative cases: the device must
refuse a broker whose certificate comes from an unknown CA, and the broker must
refuse a device without a certificate.

## Resource use (nucleo_h563zi, Zephyr 3.7.2, SDK 0.16.8)

| | Flash | RAM (SRAM1, 256 KB) |
|---|---|---|
| MQTT + TLS (default) | 209 KB | 138 KB |

The RAM figure includes a 64 KB mbedTLS heap, sized for full 16 KB TLS records
(see `prj.conf`), and an 8 KB main stack for the TLS handshake.

## Security notes

- The broker certificate is verified by default: the CA and the host name are
  checked (`CONFIG_APP_MQTT_TLS_PEER_VERIFY`). Certificate validity dates are
  **not** checked, because the board has no trusted time source at boot. Add
  SNTP and `CONFIG_MBEDTLS_HAVE_TIME_DATE` if expiry and not-yet-valid checks
  are needed.
- Client keys and passwords are embedded in the firmware image. On production
  parts, enable STM32 read-out protection, or move the key into a secure
  element or the STM32H573's secure storage.
- The STM32 hardware RNG feeds mbedTLS. Do not enable
  `CONFIG_TEST_RANDOM_GENERATOR`: some upstream samples do, and it makes TLS
  keys predictable on boards without an entropy driver.
