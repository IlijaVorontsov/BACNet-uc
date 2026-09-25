#!/bin/bash
# hil/pki/mkpki.sh - TEST-ONLY certificates for the HIL rig. Never use them anywhere else.
#
# Usage:
#   mkpki.sh OUTDIR    per-run certificates, signed by the committed test CA, into OUTDIR:
#                        srv-good       broker.hil.lan + IP 192.0.2.1, valid 1 year
#                        srv-wrongname  other.example (host-name check must fail)
#                        srv-expired    2020-01-01 .. 2021-01-01
#                        srv-revoked    listed in ca.crl
#                        srv-rogue      right names, signed by rogue-ca (unknown CA)
#                        client-rogue   client certificate from rogue-ca
#                        client-revoked client certificate from the test CA, listed in ca.crl
#                        ca.crl         CRL of the test CA (30 days)
#   mkpki.sh --new-ca  regenerate the committed test CA (ca.crt/ca.key, EC P-256, 10 years)
#                      and the DUT client certificate (dut-client.crt/.key, CN hil-dut).
#                      Every DUT image built for the rig must then be rebuilt.
#
# Environment: HIL_BROKER_NAME (broker.hil.lan), HIL_BROKER_IP (192.0.2.1).
# OpenSSL 3.0 (Ubuntu 24.04) has no 'x509 -not_before', so leaves are issued with 'openssl ca',
# whose -startdate/-enddate make the expired certificate.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
broker_name=${HIL_BROKER_NAME:-broker.hil.lan}
broker_ip=${HIL_BROKER_IP:-192.0.2.1}
EC=(-newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes)

die() { echo "mkpki: $*" >&2; exit 2; }

new_ca() { # key cert common-name
	openssl req -x509 "${EC[@]}" -keyout "$1" -out "$2" -days 3650 -subj "/CN=$3" \
		-addext "basicConstraints=critical,CA:TRUE" \
		-addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
}

extensions() { # config for 'openssl ca' / 'openssl x509 -extfile' (sections = profiles)
	cat <<EOF
[ca]
default_ca = hil
[hil]
dir = $1
database = \$dir/index.txt
new_certs_dir = \$dir/new
serial = \$dir/serial
crlnumber = \$dir/crlnumber
certificate = $here/ca.crt
private_key = $here/ca.key
default_md = sha256
default_days = 365
default_crl_days = 30
policy = policy
unique_subject = no
rand_serial = yes
[policy]
commonName = supplied
[server]
subjectAltName = DNS:$broker_name,IP:$broker_ip
extendedKeyUsage = serverAuth
keyUsage = critical,digitalSignature
basicConstraints = critical,CA:FALSE
[server_wrongname]
subjectAltName = DNS:other.example
extendedKeyUsage = serverAuth
keyUsage = critical,digitalSignature
basicConstraints = critical,CA:FALSE
[client]
extendedKeyUsage = clientAuth
keyUsage = critical,digitalSignature
basicConstraints = critical,CA:FALSE
EOF
}

csr() { # name common-name -> name.key name.csr in $out
	openssl req -new "${EC[@]}" -keyout "$out/$1.key" -out "$out/$1.csr" -subj "/CN=$2" 2>/dev/null
}

sign() { # name profile [openssl ca options...]
	local name=$1 profile=$2
	shift 2
	openssl ca -batch -config "$out/ca.cnf" -extensions "$profile" -notext \
		-in "$out/$name.csr" -out "$out/$name.crt" "$@" 2>/dev/null
}

sign_rogue() { # name profile
	openssl x509 -req -in "$out/$1.csr" -CA "$out/rogue-ca.crt" -CAkey "$out/rogue-ca.key" \
		-set_serial "0x$(openssl rand -hex 8)" -days 365 -sha256 \
		-extfile "$out/ca.cnf" -extensions "$2" -out "$out/$1.crt" 2>/dev/null
}

if [[ ${1:-} == --new-ca ]]; then
	out=$(mktemp -d)
	trap 'rm -rf "$out"' EXIT
	new_ca "$here/ca.key" "$here/ca.crt" "HIL TEST-ONLY Root CA"
	mkdir -p "$out/db/new" && : >"$out/db/index.txt"
	extensions "$out/db" >"$out/ca.cnf"
	csr dut-client hil-dut
	sign dut-client client -days 3600
	mv "$out/dut-client.key" "$out/dut-client.crt" "$here/"
	chmod 0644 "$here"/*.key
	echo "mkpki: new test CA and DUT client certificate in $here; rebuild every HIL DUT image"
	exit 0
fi

(($# == 1)) || die "usage: mkpki.sh OUTDIR | --new-ca"
out=$1
[[ -f $here/ca.key && -f $here/ca.crt ]] || die "committed test CA missing in $here"
mkdir -p "$out/db/new"
: >"$out/db/index.txt"
echo 1000 >"$out/db/crlnumber"
extensions "$out/db" >"$out/ca.cnf"
cp "$here/ca.crt" "$out/ca.crt"

csr srv-good "$broker_name" && sign srv-good server
csr srv-wrongname other.example && sign srv-wrongname server_wrongname
csr srv-expired "$broker_name" && sign srv-expired server -startdate 20200101000000Z -enddate 20210101000000Z
csr srv-revoked "$broker_name" && sign srv-revoked server
csr client-revoked hil-dut-revoked && sign client-revoked client

new_ca "$out/rogue-ca.key" "$out/rogue-ca.crt" "HIL TEST-ONLY Rogue CA"
csr srv-rogue "$broker_name" && sign_rogue srv-rogue server
csr client-rogue hil-dut && sign_rogue client-rogue client

for revoked in srv-revoked client-revoked; do
	openssl ca -batch -config "$out/ca.cnf" -revoke "$out/$revoked.crt" -crl_reason keyCompromise 2>/dev/null
done
openssl ca -batch -config "$out/ca.cnf" -gencrl -out "$out/ca.crl" 2>/dev/null
rm -f "$out"/*.csr
