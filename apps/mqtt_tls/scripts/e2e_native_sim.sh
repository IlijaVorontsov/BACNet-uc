#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end test of the MQTT/TLS application on native_sim (host sockets via
# NSOS) against a local mosquitto broker that requires mutual TLS.
#
# What is checked:
#   1. TLS 1.2 handshake with broker verification (CA + host name) and a client
#      certificate; the broker only accepts certificate-authenticated clients.
#   2. Retained "online" status, retained info, periodic telemetry.
#   3. Commands on .../cmd are answered on .../event (ping, unknown command,
#      oversized payload is drained without desynchronising the stream).
#   4. Reconnect with back-off after the broker restarts.
#   5. Last will: the broker publishes "offline" when the device dies.
#   6. Negative: a device that trusts a different CA refuses the broker, and a
#      device without a client certificate is refused by the broker.
#
# Requirements: west workspace with Zephyr 3.7 (see docs/SESSION_NOTES.md),
# host gcc, mosquitto, mosquitto-clients, openssl.
#
#   apps/mqtt_tls/scripts/e2e_native_sim.sh [WORK_DIR]

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$(mktemp -d /tmp/mqtt_tls_e2e.XXXXXX)}"
BOARD="native_sim/native/64"
TLS_PORT="${TLS_PORT:-18883}"
PLAIN_PORT="${PLAIN_PORT:-18884}"
CLIENT_ID="e2e-device"
ROOT="e2e"
DEV="${ROOT}/${CLIENT_ID}"

mkdir -p "${WORK}"
WORK="$(cd "${WORK}" && pwd)"
PIDS=()

log() { printf '\n=== %s\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*" >&2; dump; exit 1; }
dump() {
	for f in device.log "broker-${BROKER_RUNS:-0}.log" observer.log; do
		[ -f "${WORK}/${f}" ] && { echo "--- ${f} (tail)"; tail -n 40 "${WORK}/${f}"; }
	done
}
cleanup() {
	for pid in "${PIDS[@]}"; do kill "${pid}" 2>/dev/null || true; done
	wait 2>/dev/null || true
}
trap cleanup EXIT

# wait_for FILE REGEX TIMEOUT_S
wait_for() {
	local deadline=$((SECONDS + $3))
	while ((SECONDS < deadline)); do
		grep -Eq -- "$2" "$1" 2>/dev/null && return 0
		sleep 0.5
	done
	return 1
}

count() { grep -Ec -- "$2" "$1" 2>/dev/null || true; }

build() { # build DIR CONF
	west build -p -b "${BOARD}" -d "$1" "${APP_DIR}" -- \
		-DEXTRA_CONF_FILE="$2" >"$1.log" 2>&1 || { tail -n 60 "$1.log"; fail "build $1"; }
}

write_conf() { # write_conf FILE CA [CLIENT_CERT CLIENT_KEY]
	cat >"$1" <<EOF
CONFIG_APP_MQTT_BROKER_HOSTNAME="127.0.0.1"
CONFIG_APP_MQTT_BROKER_PORT=${TLS_PORT}
CONFIG_APP_MQTT_TLS_HOSTNAME="localhost"
CONFIG_APP_MQTT_TLS_CA_CERT_FILE="$2"
CONFIG_APP_MQTT_CLIENT_ID="${CLIENT_ID}"
CONFIG_APP_MQTT_TOPIC_ROOT="${ROOT}"
CONFIG_APP_MQTT_PUBLISH_INTERVAL_SEC=2
CONFIG_APP_MQTT_RECONNECT_MIN_MS=500
CONFIG_APP_MQTT_RECONNECT_MAX_MS=4000
CONFIG_MQTT_KEEPALIVE=5
EOF
	if [ $# -ge 4 ]; then
		cat >>"$1" <<EOF
CONFIG_APP_MQTT_TLS_CLIENT_AUTH=y
CONFIG_APP_MQTT_TLS_CLIENT_CERT_FILE="$3"
CONFIG_APP_MQTT_TLS_CLIENT_KEY_FILE="$4"
EOF
	fi
}

BROKER_RUNS=0
start_broker() {
	BROKER_RUNS=$((BROKER_RUNS + 1))
	BROKER_LOG="${WORK}/broker-${BROKER_RUNS}.log"
	mosquitto -c "${WORK}/mosquitto.conf" >"${BROKER_LOG}" 2>&1 &
	BROKER_PID=$!
	PIDS+=("${BROKER_PID}")
	wait_for "${BROKER_LOG}" "Opening ipv4 listen socket on port ${PLAIN_PORT}" 10 ||
		fail "broker did not start"
	sleep 0.5
	mosquitto_sub -h 127.0.0.1 -p "${PLAIN_PORT}" -t "${ROOT}/#" -v -q 1 \
		>>"${WORK}/observer.log" 2>&1 &
	OBSERVER_PID=$!
	PIDS+=("${OBSERVER_PID}")
	sleep 1
}

stop_broker() {
	kill "${OBSERVER_PID}" "${BROKER_PID}" 2>/dev/null || true
	wait "${OBSERVER_PID}" "${BROKER_PID}" 2>/dev/null || true
}

start_device() { # start_device BUILD_DIR LOG
	"$1/zephyr/zephyr.exe" >"$2" 2>&1 &
	DEVICE_PID=$!
	PIDS+=("${DEVICE_PID}")
}

cmd() { mosquitto_pub -h 127.0.0.1 -p "${PLAIN_PORT}" -q 1 -t "${DEV}/cmd" -m "$1"; }

log "Work directory: ${WORK}"
command -v west >/dev/null || fail "west not found (activate the Zephyr venv)"
command -v mosquitto >/dev/null || fail "mosquitto not found"

log "Generating PKI"
CLIENT_CN="${CLIENT_ID}" "${APP_DIR}/scripts/gen_dev_certs.sh" "${WORK}/pki" >/dev/null
"${APP_DIR}/scripts/gen_dev_certs.sh" "${WORK}/rogue" >/dev/null # unrelated CA
# mosquitto drops root privileges before loading its key; test PKI only.
chmod 644 "${WORK}/pki/server.key"

cat >"${WORK}/mosquitto.conf" <<EOF
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

log "Building (${BOARD})"
write_conf "${WORK}/good.conf" "${WORK}/pki/ca.crt" "${WORK}/pki/client.crt" "${WORK}/pki/client.key"
write_conf "${WORK}/rogue_ca.conf" "${WORK}/rogue/ca.crt" "${WORK}/pki/client.crt" "${WORK}/pki/client.key"
write_conf "${WORK}/no_cert.conf" "${WORK}/pki/ca.crt"
build "${WORK}/build-good" "${WORK}/good.conf"
build "${WORK}/build-rogue" "${WORK}/rogue_ca.conf"
build "${WORK}/build-nocert" "${WORK}/no_cert.conf"

log "Starting broker"
start_broker

log "1-2. Connect over mutual TLS, status/info/telemetry"
start_device "${WORK}/build-good" "${WORK}/device.log"
wait_for "${WORK}/observer.log" "^${DEV}/status online$" 30 || fail "device never came online"
wait_for "${WORK}/observer.log" "^${DEV}/info \{\"board\":\"native_sim" 10 || fail "no info message"
wait_for "${WORK}/observer.log" "^${DEV}/telemetry \{\"seq\":2," 15 || fail "telemetry not periodic"
grep -Eq "New client connected .* as ${CLIENT_ID} .*u'${CLIENT_ID}'" "${BROKER_LOG}" ||
	fail "broker did not authenticate the device by its certificate"

log "3. Commands"
cmd ping
wait_for "${WORK}/observer.log" "^${DEV}/event \{\"pong\":" 10 || fail "no pong"
cmd "self-destruct"
wait_for "${WORK}/observer.log" "^${DEV}/event \{\"error\":\"unknown command\"\}" 10 || fail "unknown command not rejected"
cmd "$(head -c 600 /dev/zero | tr '\0' 'x')"
wait_for "${WORK}/observer.log" "^${DEV}/event \{\"error\":\"payload too large\"\}" 10 || fail "oversized payload not handled"
cmd ping
deadline=$((SECONDS + 10))
until [ "$(count "${WORK}/observer.log" "^${DEV}/event \{\"pong\":")" -ge 2 ]; do
	((SECONDS < deadline)) || fail "stream desynchronised after oversized payload"
	sleep 0.5
done

log "4. Broker restart -> reconnect"
stop_broker
sleep 3
start_broker
wait_for "${WORK}/observer.log" "^${DEV}/telemetry \{\"seq\":[0-9]+,\"uptime_s\":[0-9]+,\"sessions\":2\}" 30 ||
	fail "device did not reconnect after broker restart"

log "5. Last will on abrupt device death"
kill -9 "${DEVICE_PID}"
# keep-alive 5 s -> broker gives up after 1.5 x 5 s
wait_for "${WORK}/observer.log" "^${DEV}/status offline$" 20 || fail "last will not published"

log "6a. Device must reject a broker certificate from an unknown CA"
start_device "${WORK}/build-rogue" "${WORK}/device-rogue.log"
sleep 8
kill "${DEVICE_PID}" 2>/dev/null || true
grep -q "mqtt_connect failed" "${WORK}/device-rogue.log" || fail "rogue CA: no connect failure logged"
grep -q "MQTT session established" "${WORK}/device-rogue.log" && fail "rogue CA: device connected anyway"

log "6b. Broker must reject a device without a client certificate"
start_device "${WORK}/build-nocert" "${WORK}/device-nocert.log"
sleep 8
kill "${DEVICE_PID}" 2>/dev/null || true
grep -q "MQTT session established" "${WORK}/device-nocert.log" && fail "no client cert: device connected anyway"

log "PASS"
echo "Observer transcript:"
sed -e 's/x\{40,\}/x.../' "${WORK}/observer.log" | head -n 40
