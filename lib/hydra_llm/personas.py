"""Persona loader. A persona is a markdown file with optional YAML front matter.

Example persona file (~/.config/hydra-llm/personas/friendly-tutor.md):

    ---
    model: gemma-2-2b
    temperature: 0.7
    max_tokens: 1024
    ---
    You are a friendly tutor who explains concepts simply, with examples,
    and never lectures.

The body becomes the system prompt; the front matter (optional) provides
per-persona overrides.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from . import paths


FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class Persona:
    name: str
    system_prompt: str
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    source: Optional[str] = None


def list_personas():
    """Returns dict {name: Path}. Reads from $XDG_CONFIG_HOME/hydra-llm/personas/."""
    out = {}
    if paths.PERSONAS_DIR.is_dir():
        for f in sorted(paths.PERSONAS_DIR.glob("*.md")):
            out[f.stem] = f
        for f in sorted(paths.PERSONAS_DIR.glob("*.txt")):
            if f.stem not in out:
                out[f.stem] = f
    return out


def load_persona(name_or_path: str, *, allow_inline_text: bool = False) -> Persona:
    """Loads a persona by name (looks in PERSONAS_DIR) or absolute/relative path.

    With `allow_inline_text=True`, a string that doesn't resolve to a file
    *and* clearly isn't a slug (contains whitespace, looks like prose) is
    accepted as the system prompt verbatim. This lets users write
    `hydra-llm chat --persona "Be terse and stay in character"` without
    creating a file. Slug-shaped strings still raise FileNotFoundError so
    a typo'd name doesn't silently become a one-word persona.
    """
    p = Path(name_or_path)
    if not p.is_absolute() and not p.exists():
        # Look in the personas dir.
        for ext in (".md", ".txt"):
            candidate = paths.PERSONAS_DIR / f"{name_or_path}{ext}"
            if candidate.is_file():
                p = candidate
                break
        else:
            if allow_inline_text and _looks_like_inline_prompt(name_or_path):
                return _persona_from_inline(name_or_path)
            raise FileNotFoundError(f"persona not found: {name_or_path}")
    if not p.is_file():
        raise FileNotFoundError(f"persona file does not exist: {p}")

    raw = p.read_text(encoding="utf-8")
    front_matter = {}
    body = raw
    m = FRONT_MATTER_RE.match(raw)
    if m:
        try:
            front_matter = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            front_matter = {}
        body = m.group(2)

    return Persona(
        name=p.stem,
        system_prompt=body.strip(),
        model=front_matter.get("model"),
        temperature=front_matter.get("temperature"),
        max_tokens=front_matter.get("max_tokens"),
        source=str(p),
    )


def _looks_like_inline_prompt(s: str) -> bool:
    """Distinguish a typo'd persona slug from intentional inline prompt text.

    Inline prompts almost always contain a space (multiple words) or a
    sentence-ending mark. A bare slug like 'firendly-tutor' (typo for
    friendly-tutor) has neither, so we still treat that as a missing
    file and raise.
    """
    s = s.strip()
    if not s:
        return False
    if any(ch.isspace() for ch in s):
        return True
    # Single token: only treat as inline if it has prose punctuation or
    # is unusually long (a slug-style id should be short).
    if any(ch in s for ch in ".!?,:;"):
        return True
    return False


def _persona_from_inline(text: str) -> Persona:
    return Persona(
        name="inline",
        system_prompt=text.strip(),
        source="--persona (inline text)",
    )
