#!/usr/bin/env bash
# hydra-llm one-shot installer.
#
# What this does:
#   1. Installs the hydra-llm command into ~/.local/bin (no sudo needed).
#   2. Installs catalog/personas/docker files into ~/.local/share/hydra-llm.
#   3. Runs `hydra-llm setup`, which:
#        a. Verifies Docker is reachable.
#        b. Builds the right Docker image (vulkan if /dev/dri exists, else cpu).
#        c. Downloads a small starter model (tinyllama-1.1b, ~620 MB).
#        d. Boots that model briefly to confirm everything works end-to-end.
#
# After this completes you can immediately run:
#   hydra-llm chat tinyllama-1.1b
#
# Flags:
#   --no-build        skip the docker image build (do it later with `hydra-llm setup`)
#   --no-download     skip the starter model download
#   --no-test         skip the boot/health smoke test
#   --model <id>      pick a different starter model (default: tinyllama-1.1b)
#   --skip-setup      install only, do nothing else (for automated environments)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASSTHROUGH=()
SKIP_SETUP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --no-build|--no-download|--no-test) PASSTHROUGH+=("$1"); shift ;;
        --model) PASSTHROUGH+=("$1" "$2"); shift 2 ;;
        --skip-setup) SKIP_SETUP=1; shift ;;
        -h|--help)
            sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
done

echo "==> Installing hydra-llm into ~/.local"
make -C "$HERE" --no-print-directory dev-install

# Make sure ~/.local/bin is on PATH for this session at least.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH"
       echo "    note: added \$HOME/.local/bin to PATH for this shell. Add it to"
       echo "          your ~/.bashrc / ~/.zshrc for new shells:"
       echo "          export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

# Make sure pyyaml is available for the CLI to read the catalog.
if ! python3 -c "import yaml" 2>/dev/null; then
    echo "==> Installing pyyaml (Python dependency)"
    if pip install --user --quiet pyyaml 2>/dev/null; then
        :
    elif pip install --user --break-system-packages --quiet pyyaml 2>/dev/null; then
        :
    else
        echo "    warn: could not install pyyaml automatically"
        echo "          install it manually:  sudo apt install python3-yaml"
    fi
fi

if [ "$SKIP_SETUP" = "1" ]; then
    echo "==> --skip-setup given, stopping here."
    echo "    Run 'hydra-llm setup' when you're ready to build the image and"
    echo "    fetch a starter model."
    exit 0
fi

echo
echo "==> Running first-run setup"
hydra-llm setup "${PASSTHROUGH[@]}"
