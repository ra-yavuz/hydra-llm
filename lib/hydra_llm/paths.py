"""Path resolution for hydra-llm. XDG-compliant; respects env overrides."""
import os
from pathlib import Path


def _xdg(env_var: str, default: str) -> Path:
    val = os.environ.get(env_var)
    if val:
        return Path(val).expanduser()
    return Path(os.path.expanduser(default))


# User-writable locations.
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config") / "hydra-llm"
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state") / "hydra-llm"
CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache") / "hydra-llm"
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share") / "hydra-llm"

# Conventional subdirs.
SESSIONS_DIR = STATE_DIR / "sessions"
PERSONAS_DIR = CONFIG_DIR / "personas"
MODELS_DIR_DEFAULT = DATA_DIR / "models"
USER_CATALOG = CONFIG_DIR / "catalog.yaml"
USER_CONFIG = CONFIG_DIR / "config.yaml"
# Per-alias server-launch overrides. One JSON file per alias; each
# layered on top of the global config + catalog defaults at start time.
SERVER_OVERRIDES_DIR = CONFIG_DIR / "server"

# Read-only locations searched in order. The first match wins.
SHIPPED_CATALOG_PATHS = [
    Path(os.environ.get("HYDRA_LLM_CATALOG", "")) if os.environ.get("HYDRA_LLM_CATALOG") else None,
    Path("/usr/share/hydra-llm/catalog.yaml"),
    Path("/usr/local/share/hydra-llm/catalog.yaml"),
    # Dev fallback: catalog/ in the source tree (relative to this file).
    Path(__file__).resolve().parent.parent.parent / "catalog" / "catalog.yaml",
]

SHIPPED_PRESETS_DIR_PATHS = [
    Path("/usr/share/hydra-llm/presets"),
    Path("/usr/local/share/hydra-llm/presets"),
    Path(__file__).resolve().parent.parent.parent / "presets",
]


def find_first_existing(candidates):
    for p in candidates:
        if p and Path(p).is_file():
            return Path(p)
    return None


def find_first_existing_dir(candidates):
    for p in candidates:
        if p and Path(p).is_dir():
            return Path(p)
    return None


def shipped_catalog_path():
    return find_first_existing(SHIPPED_CATALOG_PATHS)


def shipped_presets_dir():
    return find_first_existing_dir(SHIPPED_PRESETS_DIR_PATHS)


def ensure_user_dirs():
    """Create the user dirs if they don't exist. Safe to call repeatedly."""
    for d in (CONFIG_DIR, STATE_DIR, CACHE_DIR, DATA_DIR, SESSIONS_DIR,
              PERSONAS_DIR, MODELS_DIR_DEFAULT, SERVER_OVERRIDES_DIR):
        d.mkdir(parents=True, exist_ok=True)
