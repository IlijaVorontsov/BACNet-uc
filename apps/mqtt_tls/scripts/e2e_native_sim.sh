#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end test of the MQTT/TLS application on native_sim (host sockets via
# NSOS) against a local mosquitto broker that requires mutual TLS.
#
#  1. Mutual TLS: the device verifies the broker (CA + host name) and the broker
#     authenticates the device by its certificate. Status and info are retained;
#     telemetry is periodic.
#  2. Commands on .../cmd are answered on .../event, in plain text and JSON
#     (with request id echo), including identify. An oversized payload is
#     drained without desynchronising the stream (same session throughout).
#     The retained info announces fw, hwid and caps.
#  3. Dead peer: a frozen broker (SIGSTOP; the socket stays open) is detected
#     by the PINGREQ/PINGRESP timeout, and the device reconnects.
#  4. Retained commands are not replayed after a reconnect.
#  5. A broker that stalls in the middle of a large PUBLISH payload does not
#     wedge the device (bounded socket reads).
#  6. Broker restart: reconnect with growing back-off.
#  7. Last will: a frozen device is dropped by the broker's keep-alive and its
#     retained status becomes "offline".
#  8. Negative/extra TLS cases: unknown CA and wrong host name are rejected by
#     the device, a device without certificate is rejected by the broker, an
#     IP-address SAN is accepted, SNI is sent, and both TLS 1.2 and TLS 1.3
#     handshakes complete (mosquitto's tls_version is only a minimum, so the
#     broker tests above run over TLS 1.3; openssl s_server pins each version).
#  9. RSA: an RSA client key in PKCS#8 form passes the boot-time check and
#     authenticates over mutual TLS, an RSA broker certificate works over
#     TLS 1.3 (RSA-PSS), and overlay-tls12-rsa.conf reaches a TLS-1.2-only RSA
#     broker.
# 10. Runtime configuration (M5): config_get/config_set/config_reset, secrets
#     masked, immediate vs next-connect keys, topic root change + reconnect,
#     persistence across restarts, fallback to the last known good after a
#     broken broker setting.
# 11. Logs over MQTT (M6): WRN lines on <root>/<id>/log (including lines from
#     before the connection), the logs command, and the log_level setting.
# 12. SMP on UDP 1337 (M4/M5): echo, settings read (secret masked), write +
#     save + restart, factory reset; info announces mgmt/boot. (Image upload
#     and MCUboot confirmation need real hardware.)
#
# Not covered: loss of the network interface (the NSOS target has no Zephyr-
# managed interface) and the STM32 watchdog; both need the board.
#
# Requirements: west workspace with Zephyr 4.4 (see docs/SESSION_NOTES.md) and
# its venv active, host gcc, mosquitto, mosquitto-clients, openssl, and a
# Python with smpclient for the SMP steps (`pip install smpmgr`; point
# SMP_PYTHON at it, default python3).
#
#   apps/mqtt_tls/scripts/e2e_native_sim.sh [WORK_DIR]
#
# TLS_PORT, PLAIN_PORT, SNI_PORT and RSA_PORT select the local ports (defaults
# 18883, 18884, 18885, 18886).

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$(mktemp -d /tmp/mqtt_tls_e2e.XXXXXX)}"
BOARD="native_sim/native/64"
TLS_PORT="${TLS_PORT:-18883}"
PLAIN_PORT="${PLAIN_PORT:-18884}"
SNI_PORT="${SNI_PORT:-18885}"
RSA_PORT="${RSA_PORT:-18886}"
CLIENT_ID="e2e-device"
ROOT="e2e"
DEV="${ROOT}/${CLIENT_ID}"
KEEPALIVE=5
MOSQUITTO="${MOSQUITTO:-$(command -v mosquitto || echo /usr/sbin/mosquitto)}"
SMP_PYTHON="${SMP_PYTHON:-python3}"
ALT_ROOT="e2e-alt"
ALT="${ALT_ROOT}/${CLIENT_ID}"

BROKER_PID=""
OBSERVER_PID=""
DEVICE_PID=""
SSERVER_PID=""
BROKER_RUNS=0
BROKER_LOG=/dev/null
DEVICE_LOG=/dev/null

log() { printf '\n=== %s\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*" >&2; dump; exit 1; }
dump() {
	for f in "${DEVICE_LOG}" "${BROKER_LOG}" "${WORK}/observer.log"; do
		[ -f "${f}" ] && { echo "--- ${f} (tail)"; tail -n 30 "${f}" | cut -c1-200; }
	done
}
stop_pid() {
	[ -n "$1" ] || return 0
	kill -CONT "$1" 2>/dev/null || true
	kill "$1" 2>/dev/null || true
	wait "$1" 2>/dev/null || true
}
cleanup() {
	stop_pid "${DEVICE_PID}"
	stop_pid "${OBSERVER_PID}"
	stop_pid "${BROKER_PID}"
	stop_pid "${SSERVER_PID}"
}
trap cleanup EXIT

# mark FILE: number of lines so far; later checks only look after the mark.
mark() { if [ -f "$1" ]; then wc -l <"$1"; else echo 0; fi; }

# wait_for FILE MARK REGEX TIMEOUT_S: wait for REGEX in lines after MARK.
wait_for() {
	local deadline=$((SECONDS + $4))
	while ((SECONDS < deadline)); do
		tail -n +"$(($2 + 1))" "$1" 2>/dev/null | grep -Eq -- "$3" && return 0
		sleep 0.5
	done
	return 1
}

count() { grep -Ec -- "$2" "$1" 2>/dev/null || true; }

retained() { # retained TOPIC -> "<retain flag> <payload>"
	mosquitto_sub -h 127.0.0.1 -p "${PLAIN_PORT}" -t "$1" -C 1 -W 5 -F '%r %p' 2>/dev/null || true
}

pub() { mosquitto_pub -h 127.0.0.1 -p "${PLAIN_PORT}" -q 1 "$@"; }

build() { # build NAME CONF
	(cd "${APP_DIR}" && west build -p -b "${BOARD}" -d "${WORK}/build-$1" . -- \
		-DEXTRA_CONF_FILE="$2") >"${WORK}/build-$1.log" 2>&1 ||
		{ tail -n 60 "${WORK}/build-$1.log"; fail "build $1"; }
}

write_conf() { # write_conf FILE PORT HOSTNAME CA [CLIENT_CERT CLIENT_KEY]
	cat >"$1" <<EOF
CONFIG_APP_MQTT_BROKER_HOSTNAME="127.0.0.1"
CONFIG_APP_MQTT_BROKER_PORT=$2
CONFIG_APP_MQTT_TLS_HOSTNAME="$3"
CONFIG_APP_MQTT_TLS_CA_CERT_FILE="$4"
CONFIG_APP_MQTT_CLIENT_ID="${CLIENT_ID}"
CONFIG_APP_MQTT_TOPIC_ROOT="${ROOT}"
CONFIG_APP_MQTT_PUBLISH_INTERVAL_SEC=2
CONFIG_APP_MQTT_RECONNECT_MIN_MS=500
CONFIG_APP_MQTT_RECONNECT_MAX_MS=4000
CONFIG_MQTT_KEEPALIVE=${KEEPALIVE}
EOF
	if [ $# -ge 6 ]; then
		cat >>"$1" <<EOF
CONFIG_APP_MQTT_TLS_CLIENT_AUTH=y
CONFIG_APP_MQTT_TLS_CLIENT_CERT_FILE="$5"
CONFIG_APP_MQTT_TLS_CLIENT_KEY_FILE="$6"
EOF
	fi
}

start_broker() {
	BROKER_RUNS=$((BROKER_RUNS + 1))
	BROKER_LOG="${WORK}/broker-${BROKER_RUNS}.log"
	"${MOSQUITTO}" -c "${WORK}/mosquitto.conf" >"${BROKER_LOG}" 2>&1 &
	BROKER_PID=$!
	wait_for "${BROKER_LOG}" 0 "mosquitto version [0-9.]+ running" 10 &&
		kill -0 "${BROKER_PID}" 2>/dev/null || fail "broker did not start"
	mosquitto_sub -h 127.0.0.1 -p "${PLAIN_PORT}" -t "${ROOT}/#" -t "${ALT_ROOT}/#" -v -q 1 \
		>>"${WORK}/observer.log" 2>&1 &
	OBSERVER_PID=$!
	wait_for "${BROKER_LOG}" 0 "Received SUBSCRIBE from auto-" 10 || fail "observer did not subscribe"
}

stop_broker() {
	stop_pid "${OBSERVER_PID}"
	stop_pid "${BROKER_PID}"
	OBSERVER_PID=""
	BROKER_PID=""
}

start_device() { # start_device BUILD_NAME  (settings persist in flash-NAME.bin)
	DEVICE_LOG="${WORK}/device-$1.log"
	"${WORK}/build-$1/zephyr/zephyr.exe" -flash="${WORK}/flash-$1.bin" >>"${DEVICE_LOG}" 2>&1 &
	DEVICE_PID=$!
}

stop_device() {
	stop_pid "${DEVICE_PID}"
	DEVICE_PID=""
}

# ---------------------------------------------------------------------------

for t in west openssl mosquitto_sub mosquitto_pub "${MOSQUITTO}"; do
	command -v "${t}" >/dev/null || fail "${t} not found"
done
"${SMP_PYTHON}" -c "import smpclient" 2>/dev/null ||
	fail "smpclient not importable by ${SMP_PYTHON} (pip install smpmgr; set SMP_PYTHON)"
smp() { "${SMP_PYTHON}" "${APP_DIR}/scripts/smp_tool.py" 127.0.0.1 "$@" 2>/dev/null; }

mkdir -p "${WORK}"
WORK="$(cd "${WORK}" && pwd)"
chmod 0755 "${WORK}"
rm -f "${WORK}"/*.log "${WORK}"/flash-*.bin
log "Work directory: ${WORK}"

log "Generating PKI"
CLIENT_CN="${CLIENT_ID}" "${APP_DIR}/scripts/gen_dev_certs.sh" "${WORK}/pki" >/dev/null 2>&1
"${APP_DIR}/scripts/gen_dev_certs.sh" "${WORK}/rogue" >/dev/null 2>&1 # unrelated CA
KEY_TYPE=rsa CLIENT_CN="${CLIENT_ID}" "${APP_DIR}/scripts/gen_dev_certs.sh" "${WORK}/pki-rsa" >/dev/null 2>&1

# mosquitto drops root privileges before it reads its key unless told not to.
{
	if [ "$(id -u)" -eq 0 ]; then echo "user root"; fi
	cat <<EOF
per_listener_settings true
log_type all
log_dest stdout

listener ${TLS_PORT} 127.0.0.1
cafile ${WORK}/pki/ca.crt
certfile ${WORK}/pki/server.crt
keyfile ${WORK}/pki/server.key
tls_version tlsv1.2
require_certificate true
use_identity_as_username true
allow_anonymous false

listener ${RSA_PORT} 127.0.0.1
cafile ${WORK}/pki-rsa/ca.crt
certfile ${WORK}/pki-rsa/server.crt
keyfile ${WORK}/pki-rsa/server.key
require_certificate true
use_identity_as_username true
allow_anonymous false

listener ${PLAIN_PORT} 127.0.0.1
allow_anonymous true
EOF
} >"${WORK}/mosquitto.conf"

log "Building 9 variants (${BOARD})"
P="${WORK}/pki"
write_conf "${WORK}/good.conf" "${TLS_PORT}" localhost "${P}/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/ipsan.conf" "${TLS_PORT}" "" "${P}/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/rogue.conf" "${TLS_PORT}" localhost "${WORK}/rogue/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/wronghost.conf" "${TLS_PORT}" wrong.example.com "${P}/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/nocert.conf" "${TLS_PORT}" localhost "${P}/ca.crt"
write_conf "${WORK}/sni.conf" "${SNI_PORT}" localhost "${P}/ca.crt"
R="${WORK}/pki-rsa"
write_conf "${WORK}/rsaclient.conf" "${RSA_PORT}" localhost "${R}/ca.crt" "${R}/client.crt" "${R}/client.key"
write_conf "${WORK}/rsasrv.conf" "${SNI_PORT}" localhost "${R}/ca.crt"
write_conf "${WORK}/rsa12.conf" "${SNI_PORT}" localhost "${R}/ca.crt"
for b in good ipsan rogue wronghost nocert sni rsaclient rsasrv; do
	build "${b}" "${WORK}/${b}.conf"
done
build rsa12 "${APP_DIR}/overlay-tls12-rsa.conf;${WORK}/rsa12.conf"

log "Starting broker"
: >"${WORK}/observer.log"
start_broker
OBS="${WORK}/observer.log"

log "1. Mutual TLS, retained status/info, periodic telemetry"
start_device good
wait_for "${OBS}" 0 "^${DEV}/status online$" 30 || fail "device never came online"
wait_for "${OBS}" 0 "^${DEV}/info \{\"fw\":\"[0-9]+\.[0-9]+\.[0-9]+\",\"board\":\"native_sim" 10 || fail "no info message"
grep -Eq "^${DEV}/info .*\"hwid\":\"[^\"]+\".*\"caps\":\{\"cmds\":\[\"ping\",\"led\",\"identify\"," "${OBS}" ||
	fail "info lacks hwid or caps"
wait_for "${OBS}" 0 "^${DEV}/telemetry \{\"seq\":2," 15 || fail "telemetry not periodic"
grep -Eq "New client connected .* as ${CLIENT_ID} .*u'${CLIENT_ID}'" "${BROKER_LOG}" ||
	fail "broker did not authenticate the device by its certificate"
[ "$(retained "${DEV}/status")" = "1 online" ] || fail "status is not retained 'online'"
[ "$(retained "${DEV}/info" | cut -c1)" = "1" ] || fail "info is not retained"
grep -q "Subscribed (granted QoS 1)" "${DEVICE_LOG}" || fail "no SUBACK before going online"

log "2. Commands, oversized payload, stream stays in sync"
m=$(mark "${OBS}")
pub -t "${DEV}/cmd" -m ping
wait_for "${OBS}" "$m" "^${DEV}/event \{\"ok\":true,\"pong\":" 10 || fail "no pong"
pub -t "${DEV}/cmd" -m "self-destruct"
wait_for "${OBS}" "$m" "^${DEV}/event \{\"ok\":false,\"error\":\"unknown command\"\}" 10 || fail "unknown command not rejected"
pub -t "${DEV}/cmd" -m '{"id":"req-1","cmd":"led","arg":"on"}'
wait_for "${OBS}" "$m" "^${DEV}/event \{\"id\":\"req-1\",\"ok\":true,\"led\":true\}" 10 || fail "JSON led command"
pub -t "${DEV}/cmd" -m '{"id":"req-2","cmd":"identify","arg":"2"}'
wait_for "${OBS}" "$m" "^${DEV}/event \{\"id\":\"req-2\",\"ok\":true,\"identify\":2\}" 10 || fail "JSON identify command"
pub -t "${DEV}/cmd" -m "identify 0"
wait_for "${OBS}" "$m" "^${DEV}/event \{\"ok\":true,\"identify\":0\}" 10 || fail "text identify command"
pub -t "${DEV}/cmd" -m '{"id":"bad id!","cmd":"ping"}'
wait_for "${OBS}" "$m" "^${DEV}/event \{\"ok\":false,\"error\":\"invalid id\"\}" 10 || fail "invalid id not rejected"
pub -t "${DEV}/cmd" -m '{"id":"req-3"}'
wait_for "${OBS}" "$m" "^${DEV}/event \{\"id\":\"req-3\",\"ok\":false,\"error\":\"missing cmd\"\}" 10 ||
	fail "missing cmd not rejected"
pub -t "${DEV}/cmd" -m '{"cmd":'
wait_for "${OBS}" "$m" "^${DEV}/event \{\"ok\":false,\"error\":\"invalid json\"\}" 10 || fail "invalid JSON not rejected"
pub -t "${DEV}/cmd" -m "$(head -c 600 /dev/zero | tr '\0' 'x')"
wait_for "${OBS}" "$m" "^${DEV}/event \{\"ok\":false,\"error\":\"payload too large\"\}" 10 || fail "oversized payload not handled"
m2=$(mark "${OBS}")
pub -t "${DEV}/cmd" -m ping
wait_for "${OBS}" "$m2" "^${DEV}/event \{\"ok\":true,\"pong\":" 10 || fail "no pong after oversized payload"
[ "$(count "${DEVICE_LOG}" "MQTT session established")" -eq 1 ] ||
	fail "session was re-established while handling commands"

log "3+4. Frozen broker -> dead-peer detection; retained command not replayed"
pub -r -t "${DEV}/cmd" -m "led toggle" # retained; delivered live once
wait_for "${DEVICE_LOG}" 0 "Command: led toggle" 10 || fail "live command not executed"
md=$(mark "${DEVICE_LOG}")
mo=$(mark "${OBS}")
kill -STOP "${BROKER_PID}"
# keep-alive 5 s without PUBACKs -> PINGREQ -> 5 s without PINGRESP
if ! wait_for "${DEVICE_LOG}" "$md" "No PINGRESP within" 25; then
	kill -CONT "${BROKER_PID}"
	fail "dead broker not detected"
fi
kill -CONT "${BROKER_PID}"
wait_for "${OBS}" "$mo" "^${DEV}/telemetry .*\"sessions\":2\}" 30 || fail "no reconnect after dead peer"
wait_for "${DEVICE_LOG}" "$md" "Ignoring retained command" 10 || fail "retained command not flagged"
[ "$(count "${DEVICE_LOG}" "Command: led toggle")" -eq 1 ] || fail "retained command was replayed"
md=$(mark "${DEVICE_LOG}")
pub -r -n -t "${DEV}/cmd" # clear it; subscribers get an empty message
wait_for "${DEVICE_LOG}" "$md" "Ignoring empty command" 10 || fail "empty command not ignored"

log "5. Broker stalls in the middle of a large payload"
head -c $((128 * 1024 * 1024)) /dev/zero | tr '\0' 'y' >"${WORK}/big.bin"
md=$(mark "${DEVICE_LOG}")
mo=$(mark "${OBS}")
pub -t "${DEV}/cmd" -f "${WORK}/big.bin"
kill -STOP "${BROKER_PID}"
if wait_for "${DEVICE_LOG}" "$md" "Failed to read payload: -11" 25; then
	echo "stalled payload read timed out (SO_RCVTIMEO)"
elif wait_for "${DEVICE_LOG}" "$md" "No PINGRESP within" 1; then
	echo "note: payload was fully drained before the freeze; dead-peer detection ended the session"
else
	kill -CONT "${BROKER_PID}"
	fail "device wedged on a stalled payload"
fi
kill -CONT "${BROKER_PID}"
rm -f "${WORK}/big.bin"
wait_for "${OBS}" "$mo" "^${DEV}/telemetry .*\"sessions\":3\}" 40 || fail "no reconnect after payload stall"

log "6. Broker restart -> reconnect with growing back-off"
md=$(mark "${DEVICE_LOG}")
stop_broker
sleep 12
start_broker
mo=$(mark "${OBS}")
wait_for "${OBS}" "$mo" "^${DEV}/telemetry .*\"sessions\":4\}" 30 || fail "no reconnect after broker restart"
mapfile -t delays < <(tail -n +"$((md + 1))" "${DEVICE_LOG}" |
	sed -n 's/.*reconnecting in \([0-9]*\) ms.*/\1/p')
echo "back-off delays (ms): ${delays[*]}"
((${#delays[@]} >= 3)) || fail "expected at least 3 reconnect attempts, got ${#delays[@]}"
# The base doubles up to RECONNECT_MAX_MS (4000) and each delay is 75..125 %
# of it: below the cap a delay is >= 1.2x the previous one, at the cap it is
# >= 3000 ms; no delay exceeds 5000 ms.
for ((i = 1; i < ${#delays[@]}; i++)); do
	((delays[i] * 10 >= delays[i - 1] * 12 || delays[i] >= 3000)) ||
		fail "back-off did not grow: ${delays[i - 1]} -> ${delays[i]} ms"
done
for d in "${delays[@]}"; do
	((d <= 5000)) || fail "back-off ${d} ms exceeds the 4000 ms cap + 25 % jitter"
done

log "7. Last will: frozen device dropped by keep-alive"
mo=$(mark "${OBS}")
mb=$(mark "${BROKER_LOG}")
kill -STOP "${DEVICE_PID}"
sleep 3
wait_for "${OBS}" "$mo" "^${DEV}/status offline$" 1 && fail "will published before keep-alive expiry"
wait_for "${OBS}" "$mo" "^${DEV}/status offline$" 20 || fail "last will not published"
wait_for "${BROKER_LOG}" "$mb" "${CLIENT_ID} has exceeded timeout" 1 || fail "will not caused by keep-alive expiry"
[ "$(retained "${DEV}/status")" = "1 offline" ] || fail "retained status is not 'offline'"
stop_device

log "8a. Device rejects a broker certificate from an unknown CA"
mb=$(mark "${BROKER_LOG}")
start_device rogue
wait_for "${DEVICE_LOG}" 0 "TLS handshake error: -0x2700" 15 || fail "rogue CA: no X.509 verification failure"
wait_for "${BROKER_LOG}" "$mb" "unknown ca" 5 || fail "rogue CA: broker saw no 'unknown ca' alert"
stop_device
! grep -q "MQTT session established" "${DEVICE_LOG}" || fail "rogue CA: device connected anyway"

log "8b. Device rejects a broker certificate for another host name"
start_device wronghost
wait_for "${DEVICE_LOG}" 0 "TLS handshake error: -0x2700" 15 || fail "wrong host: no X.509 verification failure"
stop_device
! grep -q "MQTT session established" "${DEVICE_LOG}" || fail "wrong host: device connected anyway"

log "8c. Broker rejects a device without client certificate"
mb=$(mark "${BROKER_LOG}")
start_device nocert
wait_for "${BROKER_LOG}" "$mb" "peer did not return a certificate" 15 ||
	fail "broker did not reject for a missing certificate"
# TLS 1.2 fails inside the handshake; in TLS 1.3 the client finishes first and
# gets the broker's fatal alert (-0x7780) on its first read.
wait_for "${DEVICE_LOG}" 0 "TLS handshake error|TLS data check error: -7780" 5 ||
	fail "no-cert: device saw no TLS failure"
stop_device
! grep -q "MQTT session established" "${DEVICE_LOG}" || fail "no-cert: device connected anyway"

log "8d. IP-address SAN accepted when no host name is configured"
start_device ipsan
wait_for "${DEVICE_LOG}" 0 "MQTT session established" 20 || fail "IP SAN: no session"
stop_device

# s_server_check NAME VERSION CERT_DIR BUILD EXPECTED_CIPHER_REGEX
s_server_check() {
	# s_server quits when its stdin reaches EOF, so keep stdin open.
	sleep 60 | openssl s_server -accept "${SNI_PORT}" "-tls$2" -tlsextdebug -naccept 1 \
		-cert "$3/server.crt" -key "$3/server.key" >"${WORK}/sserver-$1.log" 2>&1 &
	SSERVER_PID=$!
	sleep 1
	start_device "$4"
	wait_for "${WORK}/sserver-$1.log" 0 'TLS client extension "server name"' 15 ||
		fail "$1: no SNI in ClientHello"
	wait_for "${WORK}/sserver-$1.log" 0 "CIPHER is $5" 15 || fail "$1: handshake did not complete"
	stop_device
	stop_pid "${SSERVER_PID}"
	SSERVER_PID=""
}

log "8e. TLS 1.2 handshake with SNI"
s_server_check tls12 1_2 "${P}" sni 'ECDHE-ECDSA-AES(128|256)-GCM-SHA(256|384)'
log "8f. TLS 1.3 handshake with SNI"
s_server_check tls13 1_3 "${P}" sni 'TLS_AES_(128|256)_GCM_SHA(256|384)'

log "9a. RSA client key (PKCS#8) over mutual TLS"
mb=$(mark "${BROKER_LOG}")
start_device rsaclient
wait_for "${DEVICE_LOG}" 0 "MQTT session established" 20 || fail "RSA client: no session"
wait_for "${BROKER_LOG}" "$mb" "New client connected .* as ${CLIENT_ID} .*u'${CLIENT_ID}'" 5 ||
	fail "RSA client: broker did not authenticate the RSA certificate"
! grep -q "does not match\|cannot be parsed" "${DEVICE_LOG}" || fail "RSA client: boot check rejected the key"
stop_device

log "9b. RSA broker certificate over TLS 1.3 (RSA-PSS)"
s_server_check rsa13 1_3 "${R}" rsasrv 'TLS_AES_(128|256)_GCM_SHA(256|384)'

log "9c. TLS-1.2-only RSA broker with overlay-tls12-rsa.conf"
s_server_check rsa12 1_2 "${R}" rsa12 'ECDHE-RSA-AES(128|256)-GCM-SHA(256|384)'

# cmd_to ROOT_DEV MESSAGE; ev_wait MARK REGEX [TIMEOUT]: event reply after MARK
cmd_to() { pub -t "$1/cmd" -m "$2"; }
ev_wait() { wait_for "${OBS}" "$1" "$2" "${3:-10}"; }
online_after() { wait_for "${OBS}" "$1" "^$2/status online$" "${3:-30}"; }

log "10. Runtime configuration (M5)"
rm -f "${WORK}/flash-good.bin"
mo=$(mark "${OBS}")
start_device good
online_after "$mo" "${DEV}" || fail "config: device not online"
mo=$(mark "${OBS}")
cmd_to "${DEV}" config_get
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"config\":\{\"broker_host\":\"127\.0\.0\.1\",\"broker_port\":${TLS_PORT},.*\"password\":\"\",\"topic_root\":\"${ROOT}\",\"publish_interval\":2,\"keepalive\":${KEEPALIVE},\"log_level\":\"wrn\"\}\}" ||
	fail "config_get: unexpected configuration"
cmd_to "${DEV}" '{"id":"c1","cmd":"config_set","arg":"password=s3cret"}'
ev_wait "$mo" "^${DEV}/event \{\"id\":\"c1\",\"ok\":true,\"key\":\"password\",\"applies\":\"next_connect\"\}" ||
	fail "config_set password"
cmd_to "${DEV}" "config_get password"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"password\",\"value\":\"\*\*\*\"\}" || fail "secret not masked"
# The secret may only appear in the command itself, never in a reply.
grep "s3cret" "${OBS}" | grep -vq "^${DEV}/cmd " && fail "secret leaked on MQTT"
cmd_to "${DEV}" "config_set topic_root=bad+root"
ev_wait "$mo" "^${DEV}/event \{\"ok\":false,\"error\":\"bad value\"\}" || fail "invalid topic root accepted"
cmd_to "${DEV}" "config_set no_such_key=1"
ev_wait "$mo" "^${DEV}/event \{\"ok\":false,\"error\":\"unknown key\"\}" || fail "unknown key accepted"
cmd_to "${DEV}" "config_set password="
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"password\"" || fail "clearing password"

# publish_interval applies immediately
cmd_to "${DEV}" "config_set publish_interval=1"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"publish_interval\",\"applies\":\"now\"\}" ||
	fail "config_set publish_interval"
mt=$(mark "${OBS}")
sleep 4
n=$(tail -n +"$((mt + 1))" "${OBS}" | grep -c "^${DEV}/telemetry ")
((n >= 3)) || fail "publish_interval=1 not applied immediately ($n telemetry in 4 s)"

# topic root: next connect; reconnect applies it
cmd_to "${DEV}" "config_set topic_root=${ALT_ROOT}"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"topic_root\",\"applies\":\"next_connect\"\}" ||
	fail "config_set topic_root"
ma=$(mark "${OBS}")
cmd_to "${DEV}" reconnect
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"reconnect\":true\}" || fail "reconnect reply"
online_after "$ma" "${ALT}" || fail "not online under the new topic root"
cmd_to "${ALT}" ping
ev_wait "$ma" "^${ALT}/event \{\"ok\":true,\"pong\":" || fail "no pong under the new topic root"

# persistence across a restart
stop_device
ma=$(mark "${OBS}")
start_device good
online_after "$ma" "${ALT}" || fail "topic root not persisted across restart"
cmd_to "${ALT}" "config_get publish_interval"
ev_wait "$ma" "^${ALT}/event \{\"ok\":true,\"key\":\"publish_interval\",\"value\":\"1\"\}" ||
	fail "publish_interval not persisted"
cmd_to "${ALT}" "config_set topic_root=${ROOT}"
ev_wait "$ma" "^${ALT}/event \{\"ok\":true,\"key\":\"topic_root\"" || fail "restore topic root"
mo=$(mark "${OBS}")
cmd_to "${ALT}" reconnect
online_after "$mo" "${DEV}" || fail "not back under the original root"

# a broken broker setting falls back to the last known good
md=$(mark "${DEVICE_LOG}")
cmd_to "${DEV}" "config_set broker_port=1"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"broker_port\",\"applies\":\"next_connect\"\}" ||
	fail "config_set broker_port"
mf=$(mark "${OBS}")
cmd_to "${DEV}" reconnect
wait_for "${DEVICE_LOG}" "$md" "fell back to the last known good configuration" 60 ||
	fail "no fallback after a broken broker setting"
online_after "$mf" "${DEV}" 30 || fail "not online again after fallback"
fails=$(tail -n +"$((md + 1))" "${DEVICE_LOG}" | grep -c "Broker 127.0.0.1 -> 127.0.0.1:1$")
((fails == 5)) || fail "expected 5 attempts with the broken setting, saw ${fails}"
mo=$(mark "${OBS}")
cmd_to "${DEV}" "config_get broker_port"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"broker_port\",\"value\":\"${TLS_PORT}\"\}" ||
	fail "broker_port not rolled back"

log "11. Logs over MQTT (M6)"
# The fallback warning was logged while offline; it must arrive now.
wait_for "${OBS}" "$mf" "^${DEV}/log \{\"t\":[0-9]+,\"lvl\":\"wrn\",\"src\":\"app\",\"msg\":\"New connection settings failed 5 times: fell back to the last known good configuration\"\}" 15 ||
	fail "warning from before the connection not published on the log topic"
mo=$(mark "${OBS}")
cmd_to "${DEV}" "$(head -c 300 /dev/zero | tr '\0' 'z')"
wait_for "${OBS}" "$mo" "^${DEV}/log \{\"t\":[0-9]+,\"lvl\":\"wrn\",\"src\":\"app\",\"msg\":\"Command of 300 bytes discarded \(max 128\)\"\}" 10 ||
	fail "WRN not published on the log topic"
cmd_to "${DEV}" '{"id":"l1","cmd":"logs","arg":"3"}'
ev_wait "$mo" "^${DEV}/event \{\"id\":\"l1\",\"ok\":true,\"logs\":\[.*Command of 300 bytes discarded.*\]\}" ||
	fail "logs command"
tail -n +"$((mo + 1))" "${OBS}" | grep -q "^${DEV}/log .*\"lvl\":\"inf\"" && fail "INF published at level wrn"
cmd_to "${DEV}" "config_set log_level=inf"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"log_level\",\"applies\":\"now\"\}" || fail "config_set log_level"
wait_for "${OBS}" "$mo" "^${DEV}/log \{\"t\":[0-9]+,\"lvl\":\"inf\",\"src\":\"app\",\"msg\":\"Published telemetry #" 10 ||
	fail "log_level=inf not applied"
cmd_to "${DEV}" '{"id":"l2","cmd":"config_set","arg":"password=t0psecret"}'
ev_wait "$mo" "^${DEV}/event \{\"id\":\"l2\",\"ok\":true" || fail "config_set password at log_level inf"
sleep 2
grep "t0psecret" "${OBS}" | grep -vq "^${DEV}/cmd " && fail "secret leaked on the log topic"
grep -q "t0psecret" "${DEVICE_LOG}" && fail "secret leaked in the device log"
cmd_to "${DEV}" "config_set log_level=wrn"

log "12. SMP on UDP 1337 (M4/M5)"
[ "$(smp echo hello)" = "hello" ] || fail "SMP echo"
[ "$(smp read mqtt/broker_host)" = "127.0.0.1" ] || fail "SMP settings read"
mo=$(mark "${OBS}")
cmd_to "${DEV}" "config_set password=pw"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"password\"" || fail "config_set password"
[ "$(smp read mqtt/password)" = "***" ] || fail "SMP read of a secret not masked"
grep -Eq "^${DEV}/info .*\"mgmt\":\{\"smp\":\"udp:1337\"\},\"boot\":\"none\".*\"caps\":\{\"cmds\":\[\"ping\",\"led\",\"identify\",\"config\",\"reconnect\",\"logs\"\],\"config\":\[\"broker_host\"" "${OBS}" ||
	fail "info lacks mgmt/boot or caps.config"
[ "$(smp write mqtt/publish_interval 4)" = "ok" ] || fail "SMP settings write"
[ "$(smp save)" = "ok" ] || fail "SMP settings save"
cmd_to "${DEV}" "config_get publish_interval"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"publish_interval\",\"value\":\"4\"\}" || fail "SMP write not applied"
stop_device
mo=$(mark "${OBS}")
start_device good
online_after "$mo" "${DEV}" || fail "not online after restart"
cmd_to "${DEV}" "config_get publish_interval"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"publish_interval\",\"value\":\"4\"\}" || fail "SMP save not persisted"
[ "$(smp factory-reset)" = "ok" ] || fail "SMP factory reset"
sleep 1
cmd_to "${DEV}" config_get
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"config\":\{.*\"password\":\"\",.*\"publish_interval\":2," || fail "factory reset did not restore the defaults"
stop_device
mo=$(mark "${OBS}")
start_device good
online_after "$mo" "${DEV}" || fail "not online after factory reset + restart"
cmd_to "${DEV}" "config_get publish_interval"
ev_wait "$mo" "^${DEV}/event \{\"ok\":true,\"key\":\"publish_interval\",\"value\":\"2\"\}" || fail "factory reset not persisted"
stop_device

log "PASS"
echo "Observer transcript (first lines):"
sed -e 's/\(xxxxxxxx\|yyyyyyyy\)[xy]*/.../' "${OBS}" | head -n 30
