"""Per-alias overrides for system prompts and sampling params.

Hydra-llm has three layers of "what should this chat behave like":

  1. Personas: standalone files in ~/.config/hydra-llm/personas/<name>.md.
     Reusable across models. Selected explicitly with `chat --persona <name>`.

  2. Per-alias overrides (this module):
        prompt: ~/.config/hydra-llm/prompts/<alias>.txt
        params: ~/.config/hydra-llm/params/<alias>.json
     Always applied for that catalog id unless `--persona` is given.

  3. Defaults (PARAM_DEFAULTS below).

Persona always wins over per-alias prompt; explicit CLI flags
(`--temperature`, `--max-tokens`) win over everything.
"""
import json
from pathlib import Path

from . import paths


PARAM_DEFAULTS = {
    "temperature": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "repeat_penalty": 1.1,
    "max_tokens": -1,   # -1 means unlimited at request time
    "seed": -1,         # -1 means pick a fresh one each request
}
PARAM_TYPES = {
    "temperature": float,
    "top_p": float,
    "top_k": int,
    "repeat_penalty": float,
    "max_tokens": int,
    "seed": int,
}


def prompts_dir() -> Path:
    return paths.CONFIG_DIR / "prompts"


def params_dir() -> Path:
    return paths.CONFIG_DIR / "params"


def prompt_path(alias: str) -> Path:
    return prompts_dir() / f"{alias}.txt"


def params_path(alias: str) -> Path:
    return params_dir() / f"{alias}.json"


def resolve_prompt(alias: str, catalog_entry: dict | None = None) -> dict:
    """Returns {source, content, path}. source is one of inline|file|none.

    Resolution:
      - inline: catalog_entry has a `system_prompt` field
      - file:   ~/.config/hydra-llm/prompts/<alias>.txt exists
      - none:   no prompt configured
    """
    p = prompt_path(alias)
    inline = (catalog_entry or {}).get("system_prompt")
    if inline:
        return {"source": "inline", "content": str(inline).strip(), "path": str(p)}
    if p.is_file():
        try:
            return {"source": "file", "content": p.read_text().strip(), "path": str(p)}
        except OSError:
            pass
    return {"source": "none", "content": "", "path": str(p)}


def resolve_params(alias: str, catalog_entry: dict | None = None) -> dict:
    """Returns {params, overrides, path}. Per-key resolution: inline > file > default.

    `overrides` maps key -> 'inline'|'file' so callers can show which keys differ
    from defaults. Missing keys are filled with PARAM_DEFAULTS and not listed in overrides.
    """
    p = params_path(alias)
    inline = (catalog_entry or {}).get("params") or {}
    file_params = {}
    if p.is_file():
        try:
            file_params = json.loads(p.read_text()) or {}
        except (OSError, json.JSONDecodeError):
            pass
    merged = dict(PARAM_DEFAULTS)
    overrides = {}
    for key in PARAM_DEFAULTS:
        if key in inline:
            merged[key] = inline[key]
            overrides[key] = "inline"
        elif key in file_params:
            merged[key] = file_params[key]
            overrides[key] = "file"
    # Coerce types in case YAML/JSON gave us int where we want float, etc.
    for key, fn in PARAM_TYPES.items():
        try:
            merged[key] = fn(merged[key])
        except (TypeError, ValueError):
            merged[key] = PARAM_DEFAULTS[key]
    return {"params": merged, "overrides": overrides, "path": str(p)}


def write_prompt(alias: str, content: str) -> Path:
    """Writes the per-alias prompt file. Returns the path."""
    p = prompt_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def clear_prompt(alias: str) -> bool:
    """Deletes the per-alias prompt file. Returns True if a file was removed."""
    p = prompt_path(alias)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def write_params(alias: str, params: dict) -> tuple[Path, dict, dict]:
    """Saves params to disk. Only known keys, coerced to the right type.
    Returns (path, saved, rejected).
    """
    p = params_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    saved = {}
    rejected = {}
    for k, v in (params or {}).items():
        if k not in PARAM_TYPES:
            rejected[k] = "unknown key"
            continue
        try:
            saved[k] = PARAM_TYPES[k](v)
        except (TypeError, ValueError):
            rejected[k] = f"could not coerce to {PARAM_TYPES[k].__name__}"
    p.write_text(json.dumps(saved, indent=2) + "\n")
    return p, saved, rejected


def clear_params(alias: str) -> bool:
    p = params_path(alias)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
