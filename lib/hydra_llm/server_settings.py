"""Per-alias server-launch overrides.

Layered resolution (narrowest wins):

  1. llama-server compiled-in defaults (we don't pass the flag at all).
  2. Catalog entry fields  (catalog.yaml). Examples:
       - extra_args:                ["--ctx-size", "32768"]
       - default_reasoning_format:  "hide"
  3. User config (config.yaml). Examples:
       - reasoning_format: "deepseek"
       - predict:           "uncapped"
  4. Per-alias override at ~/.config/hydra-llm/server/<alias>.json.
       Same keys as user config, but only for this alias. JSON keeps
       the layer machine-editable from `hydra-llm config` while
       staying human-readable.

The driver (docker_driver.start_model) calls resolve() once when
launching a model and uses the resulting dict. The CLI's `config`
subcommand reads/writes layer 4 only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import paths


# Keys we know about. Anything outside this set is rejected by the CLI
# write path so a typo can't silently land in a config file. The driver
# is more permissive (it ignores unknown keys) so user-edited files
# from a future version don't crash an older driver.
KNOWN_KEYS = {
    "reasoning_format",   # "none" | "deepseek" | "hide" | "off"
    "predict",            # "uncapped" | "off" | "<int>"
    "extra_args",         # list of additional llama-server CLI flags
    "chat_template_kwargs",  # dict, passed as JSON to --chat-template-kwargs
}

# Settings whose value affects the launch command (so changing them
# requires a restart). Currently every known key is launch-time. If we
# ever add a runtime-only key (e.g. a per-request default the driver
# can rewrite without restarting), include it here as False.
LAUNCH_TIME_KEYS = {
    "reasoning_format": True,
    "predict": True,
    "extra_args": True,
    "chat_template_kwargs": True,
}


def _override_path(alias: str) -> Path:
    return paths.SERVER_OVERRIDES_DIR / f"{alias}.json"


def load_overrides(alias: str) -> dict:
    """Read per-alias overrides JSON. Empty dict if missing or unreadable."""
    p = _override_path(alias)
    if not p.is_file():
        return {}
    try:
        with p.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_overrides(alias: str, data: dict) -> Path:
    """Write per-alias overrides JSON. Creates the directory if needed.
    If `data` is empty, removes the file (so listing is clean)."""
    p = _override_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        return p
    with p.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return p


def set_override(alias: str, key: str, value: Any) -> Path:
    """Set or unset a single key in the alias's override file. None or
    empty string drops the key. Raises ValueError on unknown keys."""
    if key not in KNOWN_KEYS:
        raise ValueError(
            f"unknown server setting: {key!r}. known: {sorted(KNOWN_KEYS)}"
        )
    data = load_overrides(alias)
    if value is None or value == "":
        data.pop(key, None)
    else:
        data[key] = value
    return save_overrides(alias, data)


def reset(alias: str, key: Optional[str] = None) -> Path:
    """Drop a single key, or the whole override file if key is None."""
    if key is None:
        return save_overrides(alias, {})
    data = load_overrides(alias)
    data.pop(key, None)
    return save_overrides(alias, data)


def resolve(alias: str, catalog_entry: dict, user_cfg: dict) -> dict:
    """Compute the effective settings used at launch time. Returns a
    dict with the same keys as KNOWN_KEYS, plus a sibling
    `_provenance` dict mapping each key to the layer that supplied it
    ("catalog", "config", "override", or "default")."""
    out: dict = {}
    prov: dict = {}

    def take(key: str, value: Any, source: str) -> None:
        if value is None or value == "":
            return
        out[key] = value
        prov[key] = source

    # Layer 2: catalog entry. Note: catalog uses `default_reasoning_format`
    # to make it explicit that the catalog value is just a default that
    # later layers can override; same for `extra_args` which is already
    # a catalog-only key.
    cat_rf = catalog_entry.get("default_reasoning_format")
    if cat_rf:
        take("reasoning_format", cat_rf, "catalog")
    cat_extra = catalog_entry.get("extra_args")
    if isinstance(cat_extra, list) and cat_extra:
        take("extra_args", list(cat_extra), "catalog")

    # Layer 3: user config (global).
    for key in KNOWN_KEYS:
        if key in ("extra_args",):
            # extra_args: append config + alias rather than replace, so
            # someone can add a flag globally without losing per-alias.
            global_extra = user_cfg.get(key)
            if isinstance(global_extra, list) and global_extra:
                merged = out.get(key, []) + list(global_extra)
                out[key] = merged
                prov[key] = (prov.get(key, "default") + "+config").strip("+")
        else:
            v = user_cfg.get(key)
            if v is not None and v != "":
                take(key, v, "config")

    # Layer 4: per-alias overrides.
    overrides = load_overrides(alias)
    for key, v in overrides.items():
        if key not in KNOWN_KEYS:
            continue
        if key == "extra_args":
            if isinstance(v, list) and v:
                merged = out.get(key, []) + list(v)
                out[key] = merged
                prov[key] = (prov.get(key, "default") + "+override").strip("+")
        else:
            take(key, v, "override")

    out["_provenance"] = prov
    return out


def diff_launch_relevant(before: dict, after: dict) -> list[str]:
    """Return the list of keys that changed in a launch-relevant way.
    Used to decide whether a running container needs a restart."""
    changed = []
    keys = (set(before) | set(after)) - {"_provenance"}
    for k in sorted(keys):
        if not LAUNCH_TIME_KEYS.get(k, True):
            continue
        if before.get(k) != after.get(k):
            changed.append(k)
    return changed
