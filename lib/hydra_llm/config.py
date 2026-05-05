"""Config and catalog loading."""
import yaml
from pathlib import Path

from . import paths


DEFAULT_CONFIG = {
    "models_dir": str(paths.MODELS_DIR_DEFAULT),
    "port_range": [18080, 18099],
    "compose_project": "hydra-llm",
    "container_prefix": "hydra-",
    # Auto-pick CPU vs Vulkan image. Override with explicit "image: vulkan|cpu".
    "image": "auto",
    # If True, list-online includes models that don't fit local hardware (just marks them).
    "show_unfit": True,
}


def load_user_config():
    if not paths.USER_CONFIG.is_file():
        return dict(DEFAULT_CONFIG)
    with open(paths.USER_CONFIG) as f:
        loaded = yaml.safe_load(f) or {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(loaded)
    return cfg


def load_catalog():
    """Loads the shipped catalog, then merges any user catalog on top.

    Returns: (catalog_list, sources_used) where sources_used is a list of file paths.
    """
    sources = []
    catalog = []
    shipped = paths.shipped_catalog_path()
    if shipped:
        with open(shipped) as f:
            data = yaml.safe_load(f) or {}
        catalog.extend(data.get("models", []))
        sources.append(str(shipped))
    if paths.USER_CATALOG.is_file():
        with open(paths.USER_CATALOG) as f:
            data = yaml.safe_load(f) or {}
        # User entries with the same id override shipped entries.
        user_models = data.get("models", [])
        by_id = {m["id"]: m for m in catalog}
        for m in user_models:
            by_id[m["id"]] = m
        catalog = list(by_id.values())
        sources.append(str(paths.USER_CATALOG))
    return catalog, sources
