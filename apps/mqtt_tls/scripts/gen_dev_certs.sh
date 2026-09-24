#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Generate a throw-away PKI for development and tests: a CA, a broker
# certificate and a device (client) certificate. Keys are ECDSA P-256, or
# RSA-2048 with KEY_TYPE=rsa (written as PKCS#8, like `openssl genpkey`).
#
#   scripts/gen_dev_certs.sh [OUT_DIR] [BROKER_NAME...]
#
# OUT_DIR defaults to certs/dev next to this script's application (git-ignored).
# BROKER_NAME values become subjectAltNames of the broker certificate; DNS names
# and IPv4 addresses are both accepted. "localhost" and 127.0.0.1 are always
# included. The device certificate's CN is the client id "dev-device" unless
# CLIENT_CN is set in the environment.
#
# Never use these keys in production: the CA key sits next to the certs.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${APP_DIR}/certs/dev}"
shift || true
CLIENT_CN="${CLIENT_CN:-dev-device}"
DAYS="${DAYS:-825}"

mkdir -p "${OUT}"
cd "${OUT}"

san="DNS:localhost,IP:127.0.0.1"
for name in "$@"; do
	if [[ "${name}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		san+=",IP:${name}"
	else
		san+=",DNS:${name}"
	fi
done

KEY_TYPE="${KEY_TYPE:-ec}"
key() {
	case "${KEY_TYPE}" in
	ec) openssl ecparam -name prime256v1 -genkey -noout -out "$1" ;;
	rsa) openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$1" 2>/dev/null ;;
	*) echo "KEY_TYPE must be ec or rsa" >&2; exit 1 ;;
	esac
}

key ca.key
openssl req -x509 -new -key ca.key -sha256 -days "${DAYS}" \
	-subj "/O=BACNet-uc dev/CN=BACNet-uc dev CA" \
	-addext "basicConstraints=critical,CA:TRUE" \
	-addext "keyUsage=critical,keyCertSign,cRLSign" \
	-out ca.crt

key server.key
openssl req -new -key server.key -subj "/O=BACNet-uc dev/CN=localhost" -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
	-days "${DAYS}" -sha256 -out server.crt -extfile <(printf '%s\n' \
	"basicConstraints=CA:FALSE" \
	"keyUsage=critical,digitalSignature" \
	"extendedKeyUsage=serverAuth" \
	"subjectAltName=${san}")

key client.key
openssl req -new -key client.key -subj "/O=BACNet-uc dev/CN=${CLIENT_CN}" -out client.csr
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
	-days "${DAYS}" -sha256 -out client.crt -extfile <(printf '%s\n' \
	"basicConstraints=CA:FALSE" \
	"keyUsage=critical,digitalSignature" \
	"extendedKeyUsage=clientAuth")

rm -f ./*.csr ./*.srl
chmod 600 ./*.key

echo "Development PKI written to ${OUT}"
echo "  broker SAN: ${san}"
echo "  device CN:  ${CLIENT_CN}"
echo "  key type:   ${KEY_TYPE}"
