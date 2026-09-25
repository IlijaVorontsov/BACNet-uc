# shellcheck shell=bash
# Container helpers for uc-controller tests (DESIGN.md §20, §19.4). Source it:
#
#   . controller/tests/lib/ctr.sh
#   ctr_build controller/tests/lib/Dockerfile.trixie uc-test-trixie
#   ctr_run uc-test-trixie python3 -m pytest -q /src/controller/tests/core
#   ctr_run --cap-add NET_ADMIN -- uc-test-raspios nft -c -f /etc/nftables.conf
#
# Conventions (this environment has an intercepting HTTPS proxy):
# - every build and run uses --network host;
# - /root/.ccr/ca-bundle.crt (when present) is copied into the build context
#   as extra-ca.crt (an empty file otherwise); only test images install it;
# - HTTPS_PROXY is passed as a build argument (and to running containers);
# - UC_TEST_PLATFORM selects the platform, default linux/arm64;
# - ctr_run mounts the repository read-only at /src and sets
#   UC_CONTROLLER_SRC=/src/controller and PYTHONPATH=/src/controller/src.
#
# Tags: arm64 images use the plain tag (uc-test-trixie = uc-test-trixie:latest),
# other platforms get the architecture as tag (uc-test-trixie:amd64), so both
# can exist side by side. Dockerfiles that build FROM a uc-test-* image should
# use "ARG UC_BASE_TAG=latest" + "FROM uc-test-trixie:${UC_BASE_TAG}";
# ctr_build passes UC_BASE_TAG for the selected platform.

CTR_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTR_REPO="$(cd "$CTR_LIB_DIR/../../.." && pwd)"
CTR_CA_BUNDLE="${CTR_CA_BUNDLE:-/root/.ccr/ca-bundle.crt}"

ctr_platform() {
    printf '%s\n' "${UC_TEST_PLATFORM:-linux/arm64}"
}

# Architecture part of the platform: linux/arm64 -> arm64
ctr_arch() {
    local p
    p="$(ctr_platform)"
    p="${p#*/}"
    printf '%s\n' "${p%%/*}"
}

# Tag for this platform: NAME -> NAME (arm64) or NAME:amd64; NAME:TAG is kept.
ctr_tag() {
    local name="$1"
    case "$name" in
        *:*) printf '%s\n' "$name" ;;
        *) if [ "$(ctr_arch)" = arm64 ]; then printf '%s\n' "$name"; else printf '%s:%s\n' "$name" "$(ctr_arch)"; fi ;;
    esac
}

ctr_base_tag() {
    if [ "$(ctr_arch)" = arm64 ]; then printf 'latest\n'; else ctr_arch; fi
}

# ctr_build DOCKERFILE TAG [CONTEXT] [-- EXTRA DOCKER BUILD ARGS...]
# CONTEXT defaults to the Dockerfile's directory. The context is copied to a
# temporary directory (without .git) so extra-ca.crt never lands in the repo.
ctr_build() {
    local dockerfile="$1" tag context tmp rc
    tag="$(ctr_tag "$2")"
    shift 2
    if [ $# -gt 0 ] && [ "$1" != "--" ]; then
        context="$1"
        shift
    else
        context="$(dirname "$dockerfile")"
    fi
    if [ "${1:-}" = "--" ]; then shift; fi
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/uc-ctx.XXXXXX")"
    tar -C "$context" --exclude=.git -cf - . | tar -C "$tmp" -xf -
    if [ -r "$CTR_CA_BUNDLE" ]; then
        cp "$CTR_CA_BUNDLE" "$tmp/extra-ca.crt"
    else
        : > "$tmp/extra-ca.crt"
    fi
    cp "$dockerfile" "$tmp/Dockerfile.uc-build"
    echo "ctr_build: $tag ($(ctr_platform)) from $dockerfile" >&2
    docker build --network host --platform "$(ctr_platform)" \
        --build-arg "HTTPS_PROXY=${HTTPS_PROXY:-}" \
        --build-arg "UC_BASE_TAG=$(ctr_base_tag)" \
        -t "$tag" -f "$tmp/Dockerfile.uc-build" "$@" "$tmp"
    rc=$?
    rm -rf "$tmp"
    return $rc
}

# ctr_run [DOCKER RUN OPTIONS... --] TAG CMD...
ctr_run() {
    local opts=() tag
    if [ "${1:-}" != "" ] && [ "${1#-}" != "$1" ]; then
        while [ $# -gt 0 ] && [ "$1" != "--" ]; do opts+=("$1"); shift; done
        [ "${1:-}" = "--" ] && shift
    fi
    tag="$(ctr_tag "$1")"
    shift
    docker run --rm --init --network host --platform "$(ctr_platform)" \
        -v "$CTR_REPO:/src:ro" \
        -e UC_CONTROLLER_SRC=/src/controller \
        -e PYTHONPATH=/src/controller/src \
        -e "HTTPS_PROXY=${HTTPS_PROXY:-}" -e "https_proxy=${HTTPS_PROXY:-}" \
        -e PYTHONDONTWRITEBYTECODE=1 \
        "${opts[@]}" "$tag" "$@"
}

# ctr_image_exists TAG -> 0 if the (platform-adjusted) image is present
ctr_image_exists() {
    docker image inspect "$(ctr_tag "$1")" >/dev/null 2>&1
}
