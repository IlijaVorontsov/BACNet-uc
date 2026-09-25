#!/bin/sh
# add-repos.sh: add the three third-party apt repositories of uc-controller
# (PGDG, packagecloud Timescale, repo.mosquitto.org) with pinned keys and the
# apt pins of DESIGN.md §4.10. Used by the image build, the lab installer and
# the test containers.
#
#   sudo ./add-repos.sh                                  # then: apt-get update
#   sudo APT_HTTPS_PROXY=http://proxy:3128 ./add-repos.sh
#
# Idempotent: every run installs the same files again. Each keyring is
# checked against FINGERPRINTS before it is installed. With APT_HTTPS_PROXY
# set, /etc/apt/apt.conf.d/90uc-proxy gets Acquire::https::Proxy.
set -eu

here=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" != 0 ]; then
    echo "add-repos.sh: run as root" >&2
    exit 1
fi
if ! command -v gpg >/dev/null 2>&1; then
    echo "add-repos.sh: gpg (package gnupg) is needed to check the key fingerprints" >&2
    exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

install -d -m 0755 /etc/apt/keyrings /etc/apt/sources.list.d /etc/apt/preferences.d /etc/apt/apt.conf.d

while read -r name fpr _source; do
    case "$name" in '' | '#'*) continue ;; esac
    key="$here/keyrings/$name.asc"
    if [ ! -r "$key" ]; then
        echo "add-repos.sh: missing keyring $key" >&2
        exit 1
    fi
    # exactly one primary key, with the pinned fingerprint
    listing=$(GNUPGHOME="$tmp" gpg --batch --quiet --with-colons --show-keys "$key" 2>/dev/null)
    primaries=$(printf '%s\n' "$listing" | grep -c '^pub:' || true)
    got=$(printf '%s\n' "$listing" | awk -F: '$1 == "pub" { want = 1; next } want && $1 == "fpr" { print $10; exit }')
    if [ "$primaries" != 1 ] || [ "$got" != "$fpr" ]; then
        echo "add-repos.sh: $name: key fingerprint ${got:-?} ($primaries primary keys), expected $fpr" >&2
        exit 1
    fi
    install -m 0644 "$key" "/etc/apt/keyrings/uc-$name.asc"
done < "$here/FINGERPRINTS"

for src in "$here"/sources/*.sources; do
    install -m 0644 "$src" "/etc/apt/sources.list.d/$(basename "$src")"
done
install -m 0644 "$here/uc-controller.pref" /etc/apt/preferences.d/uc-controller.pref

if [ -n "${APT_HTTPS_PROXY:-}" ]; then
    printf 'Acquire::https::Proxy "%s";\n' "$APT_HTTPS_PROXY" > /etc/apt/apt.conf.d/90uc-proxy
    chmod 0644 /etc/apt/apt.conf.d/90uc-proxy
fi

echo "add-repos.sh: PGDG, Timescale and mosquitto repositories installed; run apt-get update"
