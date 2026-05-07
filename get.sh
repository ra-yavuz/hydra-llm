#!/usr/bin/env bash
# hydra-llm one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/ra-yavuz/hydra-llm/main/get.sh | bash
#
# This script does NOT need root. It installs into your user's ~/.local.
# It will use sudo only if it needs to install missing system packages
# (python3-yaml, docker.io). It will tell you what it's about to sudo for and
# you can hit Ctrl+C to abort.
#
# What it does:
#   1. Verifies a few prerequisites (git, python3, docker).
#   2. Clones (or updates) the repo into ~/.local/share/hydra-llm/source.
#   3. Runs make user-install to copy hydra-llm into ~/.local/bin.
#   4. Runs hydra-llm setup to build the Docker image, fetch a starter model,
#      and run a smoke test.
#   5. Prints a summary of what worked and what to try next.
#
# Flags (pass at the end of the curl pipe):
#   curl ... | bash -s -- --no-test --no-download
#
#   --branch <name>      git branch/tag to install (default: main)
#   --no-build           skip the Docker image build
#   --no-download        skip the starter model download
#   --no-test            skip the start/stop smoke test
#   --model <id>         starter model id (default: tinyllama-1.1b)
#   --reinstall          force re-clone of the source tree
#
# To uninstall later: hydra-llm uninstall (keeps data) or hydra-llm wipe (deletes everything).

set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    cat >&2 <<'ERR'
hydra-llm get.sh: do NOT run me as root.

The installer puts files into your user's ~/.local. Running as root would put
them into /root/.local instead, and your Docker images would be owned by root.

If you piped me through "sudo bash", just remove the sudo:
    curl -fsSL https://raw.githubusercontent.com/ra-yavuz/hydra-llm/main/get.sh | bash
ERR
    exit 1
fi

# Defaults
BRANCH="main"
REPO_URL="https://github.com/ra-yavuz/hydra-llm.git"
SRC_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hydra-llm/source"
PASSTHROUGH=()
REINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --reinstall) REINSTALL=1; shift ;;
        --no-build|--no-download|--no-test) PASSTHROUGH+=("$1"); shift ;;
        --model) PASSTHROUGH+=("$1" "$2"); shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m  warn  %s\033[0m\n' "$*" >&2; }
fail() { printf '\033[31m  fail  %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. Prerequisites ---
say "Checking prerequisites"

need() {
    local tool="$1" pkg="$2"
    if ! command -v "$tool" >/dev/null 2>&1; then
        warn "missing: $tool"
        if [ -n "$pkg" ] && command -v apt-get >/dev/null 2>&1; then
            note "I will run: sudo apt-get install -y $pkg"
            sudo apt-get update -qq
            sudo apt-get install -y "$pkg"
        else
            fail "please install $tool and re-run get.sh"
        fi
    fi
}

need git git
need python3 python3
need make make

# pyyaml: try import, then user-pip, then apt.
if ! python3 -c "import yaml" 2>/dev/null; then
    if pip install --user --quiet pyyaml 2>/dev/null \
       || pip install --user --break-system-packages --quiet pyyaml 2>/dev/null; then
        :
    elif command -v apt-get >/dev/null 2>&1; then
        note "I will run: sudo apt-get install -y python3-yaml"
        sudo apt-get install -y python3-yaml
    else
        fail "could not install pyyaml; install it manually then re-run"
    fi
fi

# Docker is the engine hydra-llm runs every model in. It is required at
# runtime; we offer to apt-install on Debian/Ubuntu, otherwise point at
# upstream docs. We do *not* auto-add the user to the docker group: that
# requires a logout/login to take effect, which we can't do from this
# script, so the user has to do it themselves.
if ! command -v docker >/dev/null 2>&1; then
    warn "docker is not installed; hydra-llm needs it to run models."
    if command -v apt-get >/dev/null 2>&1; then
        note "I can install it now via:  sudo apt-get install -y docker.io"
        if [ -t 0 ]; then
            read -rp "    Install docker.io now? [Y/n] " ans
            ans=${ans:-y}
        else
            ans=y
        fi
        case "${ans,,}" in
            y|yes)
                sudo apt-get install -y docker.io || \
                    warn "apt-get install docker.io failed; install manually"
                ;;
            *)
                warn "skipping docker install. After installing it later, run:  hydra-llm setup"
                ;;
        esac
        if command -v docker >/dev/null 2>&1; then
            note "After install, add yourself to the docker group, then log out and back in:"
            note "    sudo usermod -aG docker \"\$USER\""
            note "Without that, every docker command needs sudo and the engine container will not start as your user."
        fi
    else
        warn "Install Docker (https://docs.docker.com/engine/install/), add yourself to the 'docker' group, then run:"
        warn "    hydra-llm setup"
    fi
    note "Continuing the install of hydra-llm itself."
fi

# --- 2. Clone or update the source tree ---
say "Fetching source ($BRANCH)"
mkdir -p "$(dirname "$SRC_DIR")"
if [ -d "$SRC_DIR/.git" ] && [ "$REINSTALL" != "1" ]; then
    note "updating existing checkout at $SRC_DIR"
    git -C "$SRC_DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$SRC_DIR" checkout "$BRANCH"
    git -C "$SRC_DIR" reset --hard "origin/$BRANCH"
else
    rm -rf "$SRC_DIR"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
fi

# --- 3. Install hydra-llm into ~/.local ---
say "Installing hydra-llm into ~/.local"
make -C "$SRC_DIR" --no-print-directory user-install

# Make sure ~/.local/bin is on PATH for this shell at least.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH"
       warn "$HOME/.local/bin is not on your PATH. Add this line to ~/.bashrc or ~/.zshrc:"
       warn "    export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

# --- 4. First-run setup (Docker image, starter model, smoke test) ---
SETUP_OK=0
if command -v docker >/dev/null 2>&1; then
    say "Running first-run setup"
    if hydra-llm setup "${PASSTHROUGH[@]}"; then
        SETUP_OK=1
    else
        warn "setup did not fully succeed. You can re-run it with: hydra-llm setup"
    fi
else
    warn "skipping setup because docker is not installed"
fi

# --- 5. Closing summary ---
echo
say "Done."
cat <<EOF

  hydra-llm is installed at:
    $HOME/.local/bin/hydra-llm
  Source tree:
    $SRC_DIR

  Try:
    hydra-llm doctor                            # detect your hardware
    hydra-llm list-online                       # browse the catalog
EOF

if [ "$SETUP_OK" = "1" ]; then
    cat <<EOF
    hydra-llm chat tinyllama-1.1b               # chat with the starter model
EOF
else
    cat <<EOF
    hydra-llm setup                             # finish first-run setup once docker is ready
EOF
fi

cat <<EOF

  More commands:
    hydra-llm chat <id> --persona friendly-tutor
    hydra-llm download <id>
    hydra-llm start  <id>
    hydra-llm stop   <id>
    hydra-llm api    <id>     # print the curl-ready URL

  To remove later:
    hydra-llm uninstall                         # keeps configs and downloaded models
    hydra-llm wipe                              # also deletes models, sessions, image

EOF
