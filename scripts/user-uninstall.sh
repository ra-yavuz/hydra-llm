#!/usr/bin/env bash
# User uninstall. Removes files placed by user-install.sh.
#
# Usage:
#   user-uninstall.sh <user_bin> <user_lib> <user_share> <mode>
#
# Modes:
#   keep-data   default: preserves ~/.config/hydra-llm and ~/.local/state/hydra-llm
#   wipe        also removes user data, downloaded models, sessions, and the
#               built Docker image.
set -euo pipefail

USER_BIN="${1:?missing user bin dir}"
USER_LIB="${2:?missing user lib dir}"
USER_SHARE="${3:?missing user share dir}"
MODE="${4:-keep-data}"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hydra-llm"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/hydra-llm"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/hydra-llm"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hydra-llm"

# 1. Stop any running model containers we manage.
if command -v docker >/dev/null 2>&1; then
    names=$(docker ps -a --filter 'name=^hydra-' --format '{{.Names}}' || true)
    if [ -n "$names" ]; then
        echo "Stopping running model containers:"
        echo "$names" | sed 's/^/  /'
        echo "$names" | xargs -r docker rm -f >/dev/null
    fi
fi

# 2. Remove the launcher and library.
rm -f "$USER_BIN/hydra-llm"
rm -rf "$USER_LIB"
rm -rf "$USER_SHARE"

# 3a. Plasmoid copy installed by user-install.sh, if present.
PLASMOID_USER_DIR="$HOME/.local/share/plasma/plasmoids/com.github.ra-yavuz.hydra-llm"
if [ -d "$PLASMOID_USER_DIR" ]; then
    rm -rf "$PLASMOID_USER_DIR"
    echo "Removed Plasmoid: $PLASMOID_USER_DIR"
fi

# 3. Wipe-only: also delete user data and the built Docker image.
if [ "$MODE" = "wipe" ]; then
    if command -v docker >/dev/null 2>&1; then
        # Remove built images.
        for tag in hydra-llm/llama-server:vulkan hydra-llm/llama-server:cpu; do
            if docker image inspect "$tag" >/dev/null 2>&1; then
                docker rmi -f "$tag" >/dev/null
                echo "Removed Docker image $tag"
            fi
        done
    fi
    # Optionally delete downloaded GGUF models. They can be tens of GB; ask first.
    models_dir=""
    if [ -f "$CONFIG_DIR/config.yaml" ]; then
        # Cheap parse without yaml dep.
        models_dir=$(awk -F: '/^models_dir:/{gsub(/[ "\047]/,"",$2); print $2; exit}' "$CONFIG_DIR/config.yaml" || true)
    fi
    [ -n "$models_dir" ] || models_dir="$DATA_DIR/models"
    rm -rf "$CONFIG_DIR" "$STATE_DIR" "$CACHE_DIR" "$DATA_DIR" "$models_dir"
    echo "Wiped user data: $CONFIG_DIR, $STATE_DIR, $CACHE_DIR, $DATA_DIR"
fi

if [ "$MODE" = "keep-data" ]; then
    cat <<EOF
Uninstalled. User data preserved:
  $CONFIG_DIR  (config, custom personas)
  $STATE_DIR   (chat sessions)
  ~/.local/share/hydra-llm/models  (downloaded GGUFs)
  Docker image hydra-llm/llama-server:* (run 'docker rmi' to remove)

To wipe everything: 'hydra-llm wipe'  (or re-run with: --wipe)
EOF
else
    echo "Wipe complete."
fi
