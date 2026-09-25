#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# A small certificate authority for one site's MQTT broker: the broker
# certificate, the hub's client certificate and one client certificate per
# board (mutual TLS). ECDSA P-256, as in apps/mqtt_tls.
#
#   deploy/mqtt-pki.sh init  DIR BROKER_NAME...   # CA + broker certificate
#   deploy/mqtt-pki.sh issue DIR CN               # client certificate CN.crt/CN.key
#
# BROKER_NAME values (DNS names or IPv4 addresses) become subjectAltNames of the
# broker certificate; boards verify the broker against them, so list the name or
# address the boards use in CONFIG_APP_MQTT_BROKER_HOSTNAME. For the hub, issue
# CN "uc-hub"; for a board, issue its MQTT client ID (the broker ACL in
# deploy/mosquitto/uc-hub.acl uses the CN as the user name).
#
# ca.key never leaves DIR. Keep DIR offline or at least readable by root only.

set -euo pipefail

usage() {
	sed -n '5,17p' "$0" | sed 's/^# \{0,1\}//'
	exit 2
}

[[ $# -ge 2 ]] || usage
cmd="$1"
dir="$2"
shift 2
days="${DAYS:-1825}"

mkdir -p "${dir}"
chmod 700 "${dir}"
cd "${dir}"
umask 077

ec() { openssl ecparam -name prime256v1 -genkey -noout -out "$1"; }

sign() { # name cn extfile
	openssl req -new -key "$1.key" -subj "/CN=$2" -out "$1.csr"
	openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
		-days "${days}" -sha256 -extfile "$3" -out "$1.crt" 2>/dev/null
	rm -f "$1.csr" "$3"
	chmod 644 "$1.crt"
}

case "${cmd}" in
init)
	[[ $# -ge 1 ]] || usage
	if [[ -e ca.key ]]; then
		echo "${dir}/ca.key exists; refusing to replace the CA" >&2
		exit 1
	fi
	ec ca.key
	openssl req -x509 -new -key ca.key -sha256 -days "${days}" \
		-subj "/CN=uc-hub site MQTT CA" -out ca.crt
	chmod 644 ca.crt

	san=""
	for name in "$@"; do
		if [[ "${name}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
			san+="${san:+,}IP:${name}"
		else
			san+="${san:+,}DNS:${name}"
		fi
	done
	ec broker.key
	printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature\n' \
		"${san}" > broker.ext
	sign broker "$1" broker.ext
	echo "CA and broker certificate in ${dir} (SAN ${san})"
	;;
issue)
	[[ $# -eq 1 ]] || usage
	cn="$1"
	if [[ ! "${cn}" =~ ^[A-Za-z0-9._:-]{1,64}$ ]]; then
		echo "CN must be 1-64 characters [A-Za-z0-9._:-]" >&2
		exit 1
	fi
	[[ -e ca.key ]] || { echo "run init first" >&2; exit 1; }
	if [[ -e "${cn}.crt" ]]; then
		echo "${dir}/${cn}.crt exists" >&2
		exit 1
	fi
	ec "${cn}.key"
	printf 'extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\n' > "${cn}.ext"
	sign "${cn}" "${cn}" "${cn}.ext"
	echo "client certificate ${dir}/${cn}.crt and key ${dir}/${cn}.key"
	;;
*)
	usage
	;;
esac
