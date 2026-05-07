"""Config and catalog loading."""
import yaml
from pathlib import Path

from . import paths


class CatalogError(ValueError):
    """Raised by add_user_catalog_entry for callers to format nicely."""


DEFAULT_CONFIG = {
    "models_dir": str(paths.MODELS_DIR_DEFAULT),
    "embedders_dir": str(paths.EMBEDDERS_DIR_DEFAULT),
    "port_range": [18080, 18099],
    # Separate port range for embedder sidecars so they don't compete with
    # chat-model port assignments. 19080..19099 mirrors the chat range.
    "embedder_port_range": [19080, 19099],
    # Embedder container name prefix (separate from chat-model container_prefix).
    "embedder_container_prefix": "hydra-embed-",
    # Tear an idle embedder down after this many seconds. Set to 0 to keep
    # embedders running until explicitly stopped. Default 60s gives a query
    # followed by another query enough time to reuse the warm container.
    "embedder_idle_ttl_seconds": 60,
    # RAG defaults. dual_index off means a single embedder serves all
    # chunks; flip to true (or pass --dual-index on `index`) to use a
    # separate code embedder and prose embedder fused via RRF at query
    # time. Single is faster, simpler, and adequate for personal-scale
    # corpora; dual pays off on large mixed code+prose archives.
    "rag": {
        "dual_index": False,
    },
    "compose_project": "hydra-llm",
    "container_prefix": "hydra-",
    # Auto-pick CPU vs Vulkan image. Override with explicit "image: vulkan|cpu".
    "image": "auto",
    # If True, list-online includes models that don't fit local hardware (just marks them).
    "show_unfit": True,
    # Default cap on tokens generated *when the client doesn't send max_tokens*.
    # Accepted: "uncapped" (passes --predict -1), "off" (don't pass the flag,
    # so llama-server's built-in 128 applies), or a positive integer.
    # Clients that send max_tokens always win; this only fills in the default.
    "predict": "uncapped",
    # How llama-server should expose model "thinking" / chain-of-thought.
    # Accepted:
    #   "none"     pass --reasoning-format none. Thinking stays inline in
    #              `content`, e.g. `<think>...</think>` blocks. Most plug-and-play.
    #   "deepseek" pass --reasoning-format deepseek. Thinking is split out
    #              into a separate `reasoning_content` field on each streamed
    #              delta and on the final message. Clients can render it as
    #              a fold-out.
    #   "hide"     pass --reasoning-format auto, which strips thinking on
    #              models that emit it (matches the original "thoughts not
    #              exposed" behavior).
    #   "off"      don't pass the flag at all; use llama-server's compiled-in
    #              default (varies by version).
    "reasoning_format": "none",
}


def save_user_config(cfg: dict) -> Path:
    """Write the user config back to ~/.config/hydra-llm/config.yaml.
    Returns the path written. Drops keys that match the default to keep the
    file small and readable."""
    path = paths.USER_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only persist values that actually differ from the built-in defaults.
    diff = {k: v for k, v in cfg.items() if DEFAULT_CONFIG.get(k) != v}
    with open(path, "w") as f:
        f.write("# hydra-llm user config. Edited by `hydra-llm config-*` "
                "commands and by hand.\n\n")
        if diff:
            yaml.safe_dump(diff, f, sort_keys=False, default_flow_style=False)
    return path


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


def add_user_catalog_entry(entry: dict, *, replace: bool = False) -> tuple[Path, bool]:
    """Append (or replace) one entry in ~/.config/hydra-llm/catalog.yaml.

    Returns (path_written, replaced_existing).
    Raises CatalogError on a duplicate id when replace=False.
    """
    if "id" not in entry or "filename" not in entry:
        raise CatalogError("entry needs at least id and filename")

    path = paths.USER_CATALOG
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {"models": []}
    if path.is_file():
        with open(path) as f:
            loaded = yaml.safe_load(f) or {}
        if isinstance(loaded, dict):
            data = loaded
            data.setdefault("models", [])
        else:
            raise CatalogError(f"{path} is not a YAML mapping; refusing to overwrite")

    models = data["models"]
    replaced = False
    for i, m in enumerate(models):
        if m.get("id") == entry["id"]:
            if not replace:
                raise CatalogError(
                    f"id {entry['id']!r} already exists in {path}. "
                    "Pass --replace to overwrite, or pick a different --id."
                )
            models[i] = entry
            replaced = True
            break
    if not replaced:
        models.append(entry)

    # yaml.safe_dump's default flow style is ugly for nested lists; use block.
    with open(path, "w") as f:
        f.write("# hydra-llm user catalog. Edited by `hydra-llm addlocal` and by hand.\n")
        f.write("# User entries override shipped entries with the same id.\n\n")
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)

    return path, replaced
