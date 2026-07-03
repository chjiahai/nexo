#!/usr/bin/env bash
# scripts/publish.sh — build multi-arch nexo images LOCALLY (no registry push).
#
# Strategy: loop platforms with `docker buildx build --load` (one image per
# arch, arch-suffixed tags), then assemble a fat manifest with
# `docker manifest`. Every arch image is immediately `docker run`-able on the
# local daemon; the fat manifest lets `docker run nexo:0.1.0` pick the host arch.
#
# Requirements:
#   - docker with buildx
#   - QEMU emulation for the non-native arch:
#       docker run --privileged --rm tonistiigi/binfmt --install all

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- Read version from pyproject.toml (line: version = "x.y.z") ---
if [[ ! -f pyproject.toml ]]; then
  echo "ERROR: pyproject.toml not found at ${REPO_ROOT}" >&2
  exit 1
fi
VERSION="$(grep -E '^version[[:space:]]*=' pyproject.toml | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
if [[ -z "${VERSION}" ]]; then
  echo "ERROR: could not extract version from pyproject.toml" >&2
  exit 1
fi
echo "==> nexo version: ${VERSION}"

IMAGE="nexo"
PLATFORMS=("linux/amd64" "linux/arm64")
ARCHES=("amd64" "arm64")

# --- Ensure a buildx builder exists (idempotent) ---
BUILDER_NAME="nexo-builder"
if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
  echo "==> creating buildx builder '${BUILDER_NAME}'"
  docker buildx create --name "${BUILDER_NAME}" --driver docker-container --use
else
  docker buildx use "${BUILDER_NAME}"
fi
docker buildx inspect --bootstrap >/dev/null

# --- Build one image per arch with --load (daemon stores single-arch images) ---
for i in "${!PLATFORMS[@]}"; do
  platform="${PLATFORMS[$i]}"
  arch="${ARCHES[$i]}"
  echo
  echo "==> building ${IMAGE}:${VERSION}-${arch}  (platform=${platform})"
  docker buildx build \
    --platform "${platform}" \
    --load \
    --tag "${IMAGE}:${VERSION}-${arch}" \
    --tag "${IMAGE}:latest-${arch}" \
    --pull \
    .
  echo "==> built ${IMAGE}:${VERSION}-${arch} and ${IMAGE}:latest-${arch}"
done

# --- Assemble fat manifests locally (no push). ---
assemble_manifest() {
  local tag="$1" amd64_ref="$2" arm64_ref="$3"
  echo
  echo "==> assembling fat manifest ${IMAGE}:${tag}"
  docker manifest rm "${IMAGE}:${tag}" 2>/dev/null || true
  docker manifest create "${IMAGE}:${tag}" "${amd64_ref}" "${arm64_ref}"
  docker manifest annotate "${IMAGE}:${tag}" "${amd64_ref}" --os linux --arch amd64
  docker manifest annotate "${IMAGE}:${tag}" "${arm64_ref}" --os linux --arch arm64
}

assemble_manifest "${VERSION}" "${IMAGE}:${VERSION}-amd64" "${IMAGE}:${VERSION}-arm64"
assemble_manifest "latest" "${IMAGE}:latest-amd64" "${IMAGE}:latest-arm64"

echo
echo "==> done. Local images:"
docker images "${IMAGE}" --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}'
echo
echo "==> run a specific arch:"
echo "    docker run --rm --platform linux/amd64 ${IMAGE}:${VERSION} nexo hello"
echo "==> or let the daemon pick via the fat manifest:"
echo "    docker run --rm ${IMAGE}:${VERSION} nexo hello"
