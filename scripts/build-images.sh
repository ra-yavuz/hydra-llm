#!/usr/bin/env bash
# Build the hydra-llm/llama-server Docker images.
# Run after `apt install hydra-llm` (or after cloning the repo for development).
#
# Variants:
#   vulkan  GPU acceleration via Vulkan (AMD/Intel/NVIDIA)
#   cpu     CPU-only with OpenBLAS
#
# By default both are attempted; pass an arg to build only one.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_DIR="$HERE/../docker"

build_one() {
    local variant="$1"
    local tag="hydra-llm/llama-server:${variant}"
    echo ">>> Building ${tag}"
    docker build \
        -f "${DOCKER_DIR}/Dockerfile.${variant}" \
        -t "${tag}" \
        "${DOCKER_DIR}"
}

case "${1:-all}" in
    vulkan|cpu) build_one "$1" ;;
    all)
        build_one cpu
        if [ -e /dev/dri ]; then
            build_one vulkan
        else
            echo ">>> Skipping vulkan image (/dev/dri not present)"
        fi
        ;;
    *)
        echo "usage: $(basename "$0") [vulkan|cpu|all]" >&2
        exit 1
        ;;
esac

echo ">>> Done. Local images:"
docker images --filter 'reference=hydra-llm/llama-server' --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}'
