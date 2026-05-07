"""Chat-side RAG: per-turn retrieval and prompt augmentation.

Lives separate from chat.py so chat.py stays a clean OpenAI-compatible
streaming client; this module does the (sometimes slow) retrieval and the
prompt-template work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RagConfig:
    """Resolved RAG configuration for a single chat session.

    Built once at chat start from CLI flags (or a catalog `rag_index:`
    field) and mutated only by /rag slash commands during the session.
    """
    enabled: bool = True
    show_attached: bool = True            # whether to print "[rag: N chunks]"
    show_chunks: bool = False             # whether to dump chunk text into the terminal
    top_k: int = 3
    code_only: bool = False
    prose_only: bool = False
    # Scope: at most one of these is set. Empty = federated across all stores.
    single_store: Optional[Path] = None   # one specific path
    store_paths: list[str] = field(default_factory=list)  # multiple explicit paths
    tags: list[str] = field(default_factory=list)         # tag-filtered federated
    federated_all: bool = False           # explicit "all stores"

    def is_single_store(self) -> bool:
        return self.single_store is not None

    def is_federated(self) -> bool:
        return not self.is_single_store()

    def scope_label(self) -> str:
        if self.single_store:
            return str(self.single_store)
        if self.store_paths:
            return f"{len(self.store_paths)} stores"
        if self.tags:
            return f"tagged {','.join(self.tags)}"
        return "all registered stores"


def attach_context(rag_cfg: RagConfig, user_text: str, *, cfg=None) -> tuple[str, list[dict]]:
    """Run retrieval for `user_text` against the configured scope, and
    return (augmented_text, hits). The augmented_text is what should be
    sent to the chat model in place of the raw user text. The original
    user_text is preserved in the saved session by the caller.

    Returns (user_text, []) if RAG is disabled, no stores match, or the
    retrieval errors -- chat is more useful with a soft-fail than with
    a hard error.
    """
    if not rag_cfg.enabled:
        return user_text, []

    from . import rag_pipeline

    try:
        if rag_cfg.is_single_store():
            hits = rag_pipeline.retrieve(
                rag_cfg.single_store,
                user_text,
                top_k=rag_cfg.top_k,
                cfg=cfg,
                code_only=rag_cfg.code_only,
                prose_only=rag_cfg.prose_only,
            )
        else:
            hits = rag_pipeline.retrieve_federated(
                user_text,
                store_paths=rag_cfg.store_paths or None,
                tags=rag_cfg.tags or None,
                top_k=rag_cfg.top_k,
                cfg=cfg,
                code_only=rag_cfg.code_only,
                prose_only=rag_cfg.prose_only,
            )
    except Exception as e:
        # Surface the failure once but don't break the chat -- the user
        # may want to keep talking even if the corpus is unreachable.
        return f"<rag-error>{e}</rag-error>\n\n{user_text}", []

    if not hits:
        return user_text, []

    blocks = []
    for h in hits:
        loc = h.get("rel_path", "?")
        line_range = f"{h.get('line_start')}-{h.get('line_end')}"
        store = h.get("store_path")
        location = f"{store}/{loc}:{line_range}" if store else f"{loc}:{line_range}"
        text = (h.get("text") or "").rstrip()
        blocks.append(f"--- {location} ---\n{text}")

    augmented = (
        "<context>\n"
        + "\n\n".join(blocks)
        + "\n</context>\n\n"
        + user_text
    )
    return augmented, hits


def render_attached_line(hits: list[dict]) -> str:
    """Compact human-readable summary line shown after the user types,
    e.g. '[rag: 3 chunks attached - lib/x.py, README.md, foo/bar.go]'.
    """
    if not hits:
        return "[rag: nothing relevant found]"
    paths = []
    for h in hits:
        store = h.get("store_path")
        rel = h.get("rel_path", "?")
        if store:
            # Only show the basename of the store to keep the line short.
            paths.append(f"{Path(store).name}:{rel}")
        else:
            paths.append(rel)
    return f"[rag: {len(hits)} chunks - {', '.join(paths)}]"
