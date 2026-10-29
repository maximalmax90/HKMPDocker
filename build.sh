#!/usr/bin/env bash
#
# Build and publish the HKMP server image.
#
#   ./build.sh                 build the latest release for this host and load
#                              it into the local docker daemon
#   ./build.sh v2.4.2          build that specific release instead
#   ./build.sh v2.4.3-2        the same server, second revision of this image
#   ./build.sh --push          build linux/amd64 + linux/arm64 and push
#
# The HKMP version is baked into the image and verified against the checksum
# GitHub publishes, so a given tag always contains exactly one known build.

set -euo pipefail

IMAGE="${IMAGE:-maximalmax90/hkmpserver}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
ALPINE_VERSION="${ALPINE_VERSION:-3.24}"
BUILDER="${BUILDER:-hkmp}"
REPO="${REPO:-Extremelyd1/HKMP}"

cd "$(dirname "$(readlink -f "$0")")"

version=
push=false
tag_latest=true

die() { printf 'build: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

while (( $# )); do
    case "$1" in
        --push)       push=true ;;
        --no-latest)  tag_latest=false ;;
        --platforms)  shift; PLATFORMS="${1:?--platforms needs a value}" ;;
        --image)      shift; IMAGE="${1:?--image needs a value}" ;;
        -h|--help)    sed -n '2,13p' "$0" | sed 's/^# \?//'; exit 0 ;;
        -*)           die "unknown option '$1'" ;;
        *)            version="$1" ;;
    esac
    shift
done

for tool in docker curl; do
    command -v "$tool" >/dev/null || die "$tool is required"
done

# Build hosts are not guaranteed to carry jq, so fall back to python3 rather
# than asking anyone to install a package for three queries.
if command -v jq >/dev/null; then
    json() {
        case "$1" in
            tag)    jq -r '.tag_name // empty' ;;
            digest) jq -r '.assets[] | select(.name == "HKMPServer.zip") | .digest // empty' ;;
        esac
    }
elif command -v python3 >/dev/null; then
    json() { python3 -c '
import json, sys
doc = json.load(sys.stdin)
if sys.argv[1] == "tag":
    print(doc.get("tag_name", ""))
else:
    for asset in doc.get("assets", []):
        if asset["name"] == "HKMPServer.zip":
            # Releases from before GitHub recorded digests report null here.
            print(asset.get("digest") or "")
            break
' "$1"; }
else
    die "either jq or python3 is required"
fi

# ---------------------------------------------------------------------------
# Resolve the release and its checksum from the GitHub API. The API reports a
# digest per asset, so nothing has to be downloaded here just to hash it.
# ---------------------------------------------------------------------------
latest="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" | json tag)"
[[ -n "$latest" && "$latest" != null ]] || die "could not resolve the latest release"

if [[ -z "$version" ]]; then
    version="$latest"
    info "no version given, using the latest release: ${version}"
fi

# A tag may carry a revision of its own: v2.4.3-2 is the second build of the
# wrapper around upstream v2.4.3. Only the part in front of that suffix names a
# release, so that is what gets fetched and checked.
if [[ "$version" =~ ^(.+)-([0-9]+)$ ]]; then
    upstream="${BASH_REMATCH[1]}"
    revision="${BASH_REMATCH[2]}"
else
    upstream="$version"
    revision=""
fi

release="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/tags/${upstream}")" \
    || die "release ${upstream} not found"

sha256="$(json digest <<<"$release" | sed 's/^sha256://')"

if [[ -z "$sha256" ]]; then
    info "the API did not report a digest, downloading the asset to hash it"
    sha256="$(curl -fsSL "https://github.com/${REPO}/releases/download/${upstream}/HKMPServer.zip" \
        | sha256sum | cut -d' ' -f1)"
fi
[[ -n "$sha256" ]] || die "could not determine the checksum of HKMPServer.zip"

# Only a build of the newest release may claim the floating tag; a new
# revision of it should take latest with it.
if [[ "$upstream" != "$latest" ]]; then
    tag_latest=false
fi

tags=(--tag "${IMAGE}:${version}")
$tag_latest && tags+=(--tag "${IMAGE}:latest")

info "release:   ${upstream}${revision:+ (image revision ${revision})}"
info "sha256:    ${sha256}"
info "image:     ${IMAGE}:${version}$($tag_latest && echo " (+ :latest)")"

# ---------------------------------------------------------------------------
# A multi-platform build needs the container driver and, on an x86 host,
# emulation for the foreign architecture. Both steps are idempotent.
# ---------------------------------------------------------------------------
if $push; then
    info "platforms: ${PLATFORMS}"

    if [[ "$PLATFORMS" == *,* ]] && [[ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]]; then
        info "installing qemu binfmt handlers"
        docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
    fi

    if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
        info "creating buildx builder '${BUILDER}'"
        docker buildx create --name "$BUILDER" --driver docker-container --bootstrap >/dev/null
    fi

    output=(--builder "$BUILDER" --platform "$PLATFORMS" --push)
else
    info "platforms: host only (pass --push to build ${PLATFORMS} and publish)"
    output=(--load)
fi

docker buildx build \
    "${output[@]}" \
    --build-arg "HKMP_VERSION=${upstream}" \
    --build-arg "IMAGE_TAG=${version}" \
    --build-arg "HKMP_SHA256=${sha256}" \
    --build-arg "ALPINE_VERSION=${ALPINE_VERSION}" \
    --label "org.opencontainers.image.revision=${version}" \
    --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "${tags[@]}" \
    .

if $push; then
    info "pushed ${IMAGE}:${version}"
else
    info "loaded ${IMAGE}:${version} into the local daemon"
fi
