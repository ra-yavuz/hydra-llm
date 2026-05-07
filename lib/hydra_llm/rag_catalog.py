"""Embedder catalog. Sibling concept to config.load_catalog() for chat models.

Embedders are a different model species: they emit fixed-size vectors instead
of text, run via `llama-server --embeddings`, and use a separate port range
from chat models so the two can run side by side without colliding.
"""
from pathlib import Path

import yaml

from . import paths


class EmbedderCatalogError(ValueError):
    """Raised by add_user_embedder_entry for callers to format nicely."""


# Required keys on every embedder entry. The pooling and query_prefix are not
# strictly required for hydra to *download* an embedder, but they are required
# to *use* one correctly -- different embedder families want different prompts
# and pooling, and getting it wrong silently degrades retrieval quality.
REQUIRED_FIELDS = ("id", "filename", "dimensions", "pooling", "kind")
VALID_KINDS = ("code", "prose", "both")
VALID_POOLINGS = ("mean", "cls", "last", "none")


def load_embedder_catalog():
    """Loads the shipped embedder catalog, merges user overrides on top.

    Returns (entries, sources_used).
    """
    sources = []
    catalog = []
    shipped = paths.shipped_embedders_path()
    if shipped:
        with open(shipped) as f:
            data = yaml.safe_load(f) or {}
        catalog.extend(data.get("embedders", []))
        sources.append(str(shipped))
    if paths.USER_EMBEDDERS.is_file():
        with open(paths.USER_EMBEDDERS) as f:
            data = yaml.safe_load(f) or {}
        user = data.get("embedders", [])
        by_id = {e["id"]: e for e in catalog}
        for e in user:
            by_id[e["id"]] = e
        catalog = list(by_id.values())
        sources.append(str(paths.USER_EMBEDDERS))
    return catalog, sources


def find_embedder(alias: str):
    """Returns the embedder entry for the given id, or None."""
    catalog, _ = load_embedder_catalog()
    return next((e for e in catalog if e.get("id") == alias), None)


def add_user_embedder_entry(entry: dict, *, replace: bool = False) -> tuple[Path, bool]:
    """Append (or replace) one entry in ~/.config/hydra-llm/embedders.yaml."""
    missing = [k for k in REQUIRED_FIELDS if k not in entry]
    if missing:
        raise EmbedderCatalogError(
            f"entry is missing required field(s): {', '.join(missing)}"
        )
    if entry["kind"] not in VALID_KINDS:
        raise EmbedderCatalogError(
            f"kind must be one of {VALID_KINDS}, got {entry['kind']!r}"
        )
    if entry["pooling"] not in VALID_POOLINGS:
        raise EmbedderCatalogError(
            f"pooling must be one of {VALID_POOLINGS}, got {entry['pooling']!r}"
        )

    path = paths.USER_EMBEDDERS
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {"embedders": []}
    if path.is_file():
        with open(path) as f:
            loaded = yaml.safe_load(f) or {}
        if isinstance(loaded, dict):
            data = loaded
            data.setdefault("embedders", [])
        else:
            raise EmbedderCatalogError(
                f"{path} is not a YAML mapping; refusing to overwrite"
            )

    entries = data["embedders"]
    replaced = False
    for i, e in enumerate(entries):
        if e.get("id") == entry["id"]:
            if not replace:
                raise EmbedderCatalogError(
                    f"id {entry['id']!r} already exists in {path}. "
                    "Pass --replace to overwrite, or pick a different --id."
                )
            entries[i] = entry
            replaced = True
            break
    if not replaced:
        entries.append(entry)

    with open(path, "w") as f:
        f.write("# hydra-llm user embedder catalog. Edited by `hydra-llm rag addlocal` and by hand.\n")
        f.write("# User entries override shipped entries with the same id.\n\n")
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)

    return path, replaced


def is_downloaded(entry: dict, cfg: dict) -> bool:
    """True if the embedder GGUF is present on disk."""
    return embedder_path(entry, cfg).is_file()


def embedder_path(entry: dict, cfg: dict) -> Path:
    """Resolve the on-disk path for an embedder GGUF."""
    embedders_dir = Path(cfg.get("embedders_dir") or paths.EMBEDDERS_DIR_DEFAULT).expanduser()
    return embedders_dir / entry["filename"]
