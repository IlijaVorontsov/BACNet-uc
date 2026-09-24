#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end test of the MQTT/TLS application on native_sim (host sockets via
# NSOS) against a local mosquitto broker that requires mutual TLS.
#
#  1. Mutual TLS: the device verifies the broker (CA + host name) and the broker
#     authenticates the device by its certificate. Status and info are retained;
#     telemetry is periodic.
#  2. Commands on .../cmd are answered on .../event. An oversized payload is
#     drained without desynchronising the stream (same session throughout).
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
#     IP-address SAN is accepted, and SNI is sent.
#
# Not covered: loss of the network interface (the NSOS target has no Zephyr-
# managed interface) and the STM32 watchdog; both need the board.
#
# Requirements: west workspace with Zephyr 3.7 (see docs/SESSION_NOTES.md) and
# its venv active, host gcc, mosquitto, mosquitto-clients, openssl.
#
#   apps/mqtt_tls/scripts/e2e_native_sim.sh [WORK_DIR]
#
# TLS_PORT, PLAIN_PORT and SNI_PORT select the local ports (defaults 18883,
# 18884, 18885).

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$(mktemp -d /tmp/mqtt_tls_e2e.XXXXXX)}"
BOARD="native_sim/native/64"
TLS_PORT="${TLS_PORT:-18883}"
PLAIN_PORT="${PLAIN_PORT:-18884}"
SNI_PORT="${SNI_PORT:-18885}"
CLIENT_ID="e2e-device"
ROOT="e2e"
DEV="${ROOT}/${CLIENT_ID}"
KEEPALIVE=5
MOSQUITTO="${MOSQUITTO:-$(command -v mosquitto || echo /usr/sbin/mosquitto)}"

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
	mosquitto_sub -h 127.0.0.1 -p "${PLAIN_PORT}" -t "${ROOT}/#" -v -q 1 \
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

start_device() { # start_device BUILD_NAME
	DEVICE_LOG="${WORK}/device-$1.log"
	"${WORK}/build-$1/zephyr/zephyr.exe" >"${DEVICE_LOG}" 2>&1 &
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

mkdir -p "${WORK}"
WORK="$(cd "${WORK}" && pwd)"
chmod 0755 "${WORK}"
rm -f "${WORK}"/*.log
log "Work directory: ${WORK}"

log "Generating PKI"
CLIENT_CN="${CLIENT_ID}" "${APP_DIR}/scripts/gen_dev_certs.sh" "${WORK}/pki" >/dev/null 2>&1
"${APP_DIR}/scripts/gen_dev_certs.sh" "${WORK}/rogue" >/dev/null 2>&1 # unrelated CA

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

listener ${PLAIN_PORT} 127.0.0.1
allow_anonymous true
EOF
} >"${WORK}/mosquitto.conf"

log "Building 6 variants (${BOARD})"
P="${WORK}/pki"
write_conf "${WORK}/good.conf" "${TLS_PORT}" localhost "${P}/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/ipsan.conf" "${TLS_PORT}" "" "${P}/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/rogue.conf" "${TLS_PORT}" localhost "${WORK}/rogue/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/wronghost.conf" "${TLS_PORT}" wrong.example.com "${P}/ca.crt" "${P}/client.crt" "${P}/client.key"
write_conf "${WORK}/nocert.conf" "${TLS_PORT}" localhost "${P}/ca.crt"
write_conf "${WORK}/sni.conf" "${SNI_PORT}" localhost "${P}/ca.crt"
for b in good ipsan rogue wronghost nocert sni; do
	build "${b}" "${WORK}/${b}.conf"
done

log "Starting broker"
: >"${WORK}/observer.log"
start_broker
OBS="${WORK}/observer.log"

log "1. Mutual TLS, retained status/info, periodic telemetry"
start_device good
wait_for "${OBS}" 0 "^${DEV}/status online$" 30 || fail "device never came online"
wait_for "${OBS}" 0 "^${DEV}/info \{\"board\":\"native_sim" 10 || fail "no info message"
wait_for "${OBS}" 0 "^${DEV}/telemetry \{\"seq\":2," 15 || fail "telemetry not periodic"
grep -Eq "New client connected .* as ${CLIENT_ID} .*u'${CLIENT_ID}'" "${BROKER_LOG}" ||
	fail "broker did not authenticate the device by its certificate"
[ "$(retained "${DEV}/status")" = "1 online" ] || fail "status is not retained 'online'"
[ "$(retained "${DEV}/info" | cut -c1)" = "1" ] || fail "info is not retained"
grep -q "Subscribed (granted QoS 1)" "${DEVICE_LOG}" || fail "no SUBACK before going online"

log "2. Commands, oversized payload, stream stays in sync"
m=$(mark "${OBS}")
pub -t "${DEV}/cmd" -m ping
wait_for "${OBS}" "$m" "^${DEV}/event \{\"pong\":" 10 || fail "no pong"
pub -t "${DEV}/cmd" -m "self-destruct"
wait_for "${OBS}" "$m" "^${DEV}/event \{\"error\":\"unknown command\"\}" 10 || fail "unknown command not rejected"
pub -t "${DEV}/cmd" -m "$(head -c 600 /dev/zero | tr '\0' 'x')"
wait_for "${OBS}" "$m" "^${DEV}/event \{\"error\":\"payload too large\"\}" 10 || fail "oversized payload not handled"
m2=$(mark "${OBS}")
pub -t "${DEV}/cmd" -m ping
wait_for "${OBS}" "$m2" "^${DEV}/event \{\"pong\":" 10 || fail "no pong after oversized payload"
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
sleep 6
start_broker
mo=$(mark "${OBS}")
wait_for "${OBS}" "$mo" "^${DEV}/telemetry .*\"sessions\":4\}" 30 || fail "no reconnect after broker restart"
mapfile -t delays < <(tail -n +"$((md + 1))" "${DEVICE_LOG}" |
	sed -n 's/.*reconnecting in \([0-9]*\) ms.*/\1/p')
echo "back-off delays (ms): ${delays[*]}"
((${#delays[@]} >= 3)) || fail "expected at least 3 reconnect attempts, got ${#delays[@]}"
((delays[1] > delays[0] && delays[2] > delays[1])) || fail "back-off does not grow"
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
wait_for "${DEVICE_LOG}" 0 "TLS handshake error" 5 || fail "no-cert: device saw no handshake failure"
stop_device
! grep -q "MQTT session established" "${DEVICE_LOG}" || fail "no-cert: device connected anyway"

log "8d. IP-address SAN accepted when no host name is configured"
start_device ipsan
wait_for "${DEVICE_LOG}" 0 "MQTT session established" 20 || fail "IP SAN: no session"
stop_device

log "8e. SNI is sent"
# s_server quits when its stdin reaches EOF, so keep stdin open.
sleep 60 | openssl s_server -accept "${SNI_PORT}" -tls1_2 -tlsextdebug -naccept 1 \
	-cert "${P}/server.crt" -key "${P}/server.key" >"${WORK}/sserver.log" 2>&1 &
SSERVER_PID=$!
sleep 1
start_device sni
wait_for "${WORK}/sserver.log" 0 'TLS client extension "server name"' 15 || fail "no SNI in ClientHello"
stop_device
stop_pid "${SSERVER_PID}"
SSERVER_PID=""

log "PASS"
echo "Observer transcript (first lines):"
sed -e 's/\(xxxxxxxx\|yyyyyyyy\)[xy]*/.../' "${OBS}" | head -n 30
