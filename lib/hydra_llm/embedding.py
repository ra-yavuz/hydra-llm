"""Thin client for the llama-server /v1/embeddings endpoint.

Hydra runs each embedder as its own container (see docker_driver.start_embedder).
This module is the in-process side: send text, get vectors back. Handles the
per-embedder query/document prefix conventions so callers don't have to remember
which family wants what.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Iterable

import numpy as np

from . import docker_driver, paths, rag_catalog


class EmbeddingError(RuntimeError):
    pass


def _post_embeddings(base_url: str, inputs: list[str], timeout: float = 60.0) -> list[list[float]]:
    """POST to base_url/v1/embeddings, return list of vectors.

    llama-server accepts an OpenAI-shaped {model, input} body and returns
    {data: [{embedding: [...]}, ...]}. We don't care about the model field.
    """
    body = json.dumps({"model": "embedder", "input": inputs}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise EmbeddingError(f"embed HTTP {e.code} {e.reason}: "
                             f"{e.read().decode('utf-8', errors='replace')[:200]}") from e
    except urllib.error.URLError as e:
        raise EmbeddingError(f"embed connection failed: {e.reason}") from e
    data = payload.get("data") or []
    if len(data) != len(inputs):
        raise EmbeddingError(
            f"embed returned {len(data)} vectors for {len(inputs)} inputs"
        )
    return [item["embedding"] for item in data]


def _apply_prefix(texts: Iterable[str], prefix: str) -> list[str]:
    if not prefix:
        return list(texts)
    return [f"{prefix}{t}" for t in texts]


def embed_documents(embedder_entry: dict, texts: list[str],
                    base_url: str | None = None,
                    batch_size: int = 32) -> np.ndarray:
    """Embed `texts` as documents (apply document_prefix from the catalog).

    Returns an (N, D) float32 numpy array. Caller is responsible for ensuring
    the embedder container is running; if base_url is None we query
    docker_driver to find it.
    """
    if not texts:
        return np.zeros((0, embedder_entry.get("dimensions", 0)), dtype=np.float32)
    if base_url is None:
        base_url = _resolve_running_url(embedder_entry)
    prefix = embedder_entry.get("document_prefix") or ""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = _apply_prefix(texts[i:i + batch_size], prefix)
        out.extend(_post_embeddings(base_url, batch))
    arr = np.asarray(out, dtype=np.float32)
    touch_embedder(embedder_entry.get("id", ""))
    return arr


def embed_query(embedder_entry: dict, text: str,
                base_url: str | None = None) -> np.ndarray:
    """Embed a single query string. Returns shape (D,) float32."""
    if base_url is None:
        base_url = _resolve_running_url(embedder_entry)
    prefix = embedder_entry.get("query_prefix") or ""
    vecs = _post_embeddings(base_url, _apply_prefix([text], prefix))
    touch_embedder(embedder_entry.get("id", ""))
    return np.asarray(vecs[0], dtype=np.float32)


def _resolve_running_url(embedder_entry: dict) -> str:
    """Look up the local URL of a running embedder by alias. Raises if not running."""
    rows, _ = docker_driver.list_running_embedders()
    match = next((r for r in rows if r["alias"] == embedder_entry["id"]
                  and r.get("state") == "running" and r.get("port")), None)
    if not match:
        raise EmbeddingError(
            f"embedder {embedder_entry['id']} is not running. "
            "Use docker_driver.ensure_embedder_running() before calling embed_*."
        )
    return f"http://127.0.0.1:{match['port']}"


def _touch_path(alias: str):
    return paths.EMBEDDER_TOUCH_DIR / alias


def touch_embedder(alias: str) -> None:
    """Record that we just used the embedder named `alias`. Idempotent;
    fail-soft (e.g. on a read-only filesystem we silently no-op so a
    chat session is never blocked by a missing touch dir).
    """
    if not alias:
        return
    p = _touch_path(alias)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Open+close is enough; we just want mtime to update. `os.utime`
        # is the right tool for that.
        if not p.exists():
            p.touch()
        else:
            os.utime(p, None)
    except OSError:
        pass


def last_touch_seconds_ago(alias: str) -> float | None:
    """Seconds since the last touch, or None if no touch on record."""
    p = _touch_path(alias)
    try:
        return time.time() - p.stat().st_mtime
    except (OSError, FileNotFoundError):
        return None


def reap_idle_embedders(cfg: dict | None = None) -> list[str]:
    """Stop embedder sidecars that haven't been touched in
    cfg['embedder_idle_ttl_seconds']. Returns the list of stopped
    aliases. Fail-soft: if anything goes wrong (no docker, no touch dir
    yet) returns []; a startup hook should never block CLI invocations.
    """
    try:
        from . import config as cfg_mod
        if cfg is None:
            cfg = cfg_mod.load_user_config()
        ttl = int(cfg.get("embedder_idle_ttl_seconds") or 0)
        if ttl <= 0:
            return []
        rows, err = docker_driver.list_running_embedders(cfg)
        if err or not rows:
            return []
        stopped = []
        for r in rows:
            if r.get("state") != "running":
                continue
            alias = r.get("alias")
            ago = last_touch_seconds_ago(alias)
            # No touch on record means this embedder was started by a
            # prior version of hydra (pre-reaper). Touch it now so we
            # don't kill it instantly; let one full TTL elapse before
            # the next run reaps it.
            if ago is None:
                touch_embedder(alias)
                continue
            if ago > ttl:
                ok, _info = docker_driver.stop_embedder(alias, cfg)
                if ok:
                    stopped.append(alias)
        return stopped
    except Exception:
        return []


def cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity between a (D,) query vector and a (N, D) matrix.
    Returns (N,). Both inputs must be float32. Hydra normalizes once at
    embed time (see normalize()), so callers can use plain dot products,
    but this helper handles the un-normalized case too.
    """
    if a.ndim != 1:
        raise ValueError("a must be 1-D (query vector)")
    if b.ndim != 2:
        raise ValueError("b must be 2-D (corpus matrix)")
    a_n = a / (np.linalg.norm(a) + 1e-12)
    b_n = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-12)
    return b_n @ a_n


def normalize(v: np.ndarray) -> np.ndarray:
    """L2-normalize a (N, D) or (D,) array in float32. Returns a fresh array."""
    v = np.asarray(v, dtype=np.float32)
    if v.ndim == 1:
        n = np.linalg.norm(v)
        return v / (n + 1e-12)
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / (n + 1e-12)
