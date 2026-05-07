#!/usr/bin/env bash
# Build the .deb packages without debhelper. Produces:
#   dist/hydra-llm_<version>_all.deb            CLI + Docker assets + uninstall script
#   dist/hydra-llm-plasma_<version>_all.deb     KDE Plasma 6 widget
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -nE '1 s/^[^(]*\(([^)]+)\).*/\1/p' "$ROOT/debian/changelog")
[ -n "$VERSION" ] || { echo "could not parse version from debian/changelog" >&2; exit 1; }

mkdir -p "$ROOT/dist"

# ---- 1. hydra-llm (CLI) ----
PKG="$ROOT/dist/hydra-llm_${VERSION}_all"
DEB="$ROOT/dist/hydra-llm_${VERSION}_all.deb"
rm -rf "$PKG" "$DEB"

mkdir -p "$PKG/DEBIAN" \
         "$PKG/usr/bin" \
         "$PKG/usr/lib/hydra-llm/hydra_llm" \
         "$PKG/usr/share/hydra-llm/personas" \
         "$PKG/usr/share/hydra-llm/docker" \
         "$PKG/usr/share/hydra-llm/scripts" \
         "$PKG/usr/share/doc/hydra-llm"

install -m 0755 "$ROOT/bin/hydra-llm"                          "$PKG/usr/bin/hydra-llm"
# Short alias for easier typing. Relative symlink, so it resolves correctly
# regardless of where the package is installed.
ln -sf hydra-llm                                               "$PKG/usr/bin/hydrallm"
install -m 0644 "$ROOT"/lib/hydra_llm/*.py                     "$PKG/usr/lib/hydra-llm/hydra_llm/"
install -m 0644 "$ROOT/catalog/catalog.yaml"                   "$PKG/usr/share/hydra-llm/catalog.yaml"
install -m 0644 "$ROOT/catalog/embedders.yaml"                 "$PKG/usr/share/hydra-llm/embedders.yaml"
install -m 0644 "$ROOT/personas/friendly-tutor.md"             "$PKG/usr/share/hydra-llm/personas/friendly-tutor.md"
install -m 0644 "$ROOT/personas/concise-coder.md"              "$PKG/usr/share/hydra-llm/personas/concise-coder.md"
install -m 0644 "$ROOT/docker/Dockerfile.vulkan"               "$PKG/usr/share/hydra-llm/docker/Dockerfile.vulkan"
install -m 0644 "$ROOT/docker/Dockerfile.cpu"                  "$PKG/usr/share/hydra-llm/docker/Dockerfile.cpu"
install -m 0755 "$ROOT/scripts/user-uninstall.sh"              "$PKG/usr/share/hydra-llm/scripts/user-uninstall.sh"
install -m 0644 "$ROOT/README.md"                              "$PKG/usr/share/doc/hydra-llm/README.md"
install -m 0644 "$ROOT/LICENSE"                                "$PKG/usr/share/doc/hydra-llm/copyright"
install -m 0755 "$ROOT/debian/hydra-llm.postinst"              "$PKG/DEBIAN/postinst"
install -m 0755 "$ROOT/debian/hydra-llm.postrm"                "$PKG/DEBIAN/postrm"

cat > "$PKG/DEBIAN/control" <<EOF
Package: hydra-llm
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-yaml, python3-pathspec, python3-numpy, docker.io | docker-ce | podman-docker, bash (>= 4.0), curl, git
Recommends: vulkan-tools, mesa-vulkan-drivers
Suggests: hydra-llm-plasma
Maintainer: Ramazan Yavuz <yavuzramazan1994@gmail.com>
Homepage: https://github.com/ra-yavuz/hydra-llm
Description: run local LLMs the easy way (CLI, with RAG)
 hydra-llm is a single-command interface for running large language models
 locally with llama.cpp packaged in Docker. Includes a curated catalog of
 community-quantized GGUFs that download anonymously, a hardware doctor
 that recommends models per system tier, persona files for repeatable chat
 personalities, persistent chat sessions, and a JSON status API used by
 the companion Plasma widget.
 .
 RAG (retrieval-augmented generation): index any folder with
 \`hydra-llm index <path>\`, query with \`hydra-llm query "..."\`, or chat
 with retrieval via \`hydra-llm chat <model> --rag <path>\`. Embedders run
 in their own llama.cpp containers on a separate port range. LanceDB
 stores vectors per-folder. Code and prose use separate embedders, with
 Reciprocal Rank Fusion across both. Cross-folder federated query and
 per-store tags supported.
 .
 Catalog-bound bundles: \`hydra-llm create <model> <persona.md> <id>
 --rag-index <path>\` bakes a model + persona + corpus into one alias.
 \`hydra-llm chat <id>\` then runs everything without flags.
 .
 Quick start: hydra-llm setup ; hydra-llm chat smollm2-135m
 .
 DISCLAIMER: provided AS IS, no warranty. Runs LLMs and Docker containers
 on your behalf, downloads multi-gigabyte files, and reads files under
 directories you index. The author is not liable for any damage to
 hardware, data, or system, or for the content of model output. See
 /usr/share/doc/hydra-llm/README.md for full text.
EOF

: > "$PKG/DEBIAN/conffiles"
dpkg-deb --build --root-owner-group "$PKG" "$DEB"
echo "Built: $DEB"

# ---- 2. hydra-llm-plasma (Plasmoid) ----
PKG2="$ROOT/dist/hydra-llm-plasma_${VERSION}_all"
DEB2="$ROOT/dist/hydra-llm-plasma_${VERSION}_all.deb"
rm -rf "$PKG2" "$DEB2"

mkdir -p "$PKG2/DEBIAN" \
         "$PKG2/usr/share/plasma/plasmoids/com.github.ra-yavuz.hydra-llm" \
         "$PKG2/usr/share/doc/hydra-llm-plasma"

install -m 0644 "$ROOT/plasmoid/metadata.json" \
        "$PKG2/usr/share/plasma/plasmoids/com.github.ra-yavuz.hydra-llm/metadata.json"
cp -r "$ROOT/plasmoid/contents" \
      "$PKG2/usr/share/plasma/plasmoids/com.github.ra-yavuz.hydra-llm/contents"
install -m 0644 "$ROOT/README.md"                              "$PKG2/usr/share/doc/hydra-llm-plasma/README.md"
install -m 0644 "$ROOT/LICENSE"                                "$PKG2/usr/share/doc/hydra-llm-plasma/copyright"

cat > "$PKG2/DEBIAN/control" <<EOF
Package: hydra-llm-plasma
Version: ${VERSION}
Section: kde
Priority: optional
Architecture: all
Depends: hydra-llm (>= ${VERSION}), plasma-workspace (>= 6.0), qml6-module-qtquick-controls, qml6-module-org-kde-kirigami, qml6-module-org-kde-plasma-components
Maintainer: Ramazan Yavuz <yavuzramazan1994@gmail.com>
Homepage: https://github.com/ra-yavuz/hydra-llm
Description: KDE Plasma 6 panel widget for hydra-llm
 A panel widget that lets you start, stop, and chat with local LLM model
 servers managed by hydra-llm.
 .
 Features: HAL 9000 inspired animated indicator (breath rate scales with
 system load), per-model start/stop/chat/logs buttons, live CPU/RAM/GPU/
 VRAM dots in the popup, inline log console, and a real-terminal chat
 spawner that picks the first available emulator from konsole, gnome-
 terminal, alacritty, kitty, xfce4-terminal, xterm.
 .
 Requires the hydra-llm CLI to be installed.
EOF

: > "$PKG2/DEBIAN/conffiles"
dpkg-deb --build --root-owner-group "$PKG2" "$DEB2"
echo "Built: $DEB2"

echo
ls -la "$ROOT/dist/"*.deb
