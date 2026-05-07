"""Orchestration: walk -> classify -> chunk -> embed -> store.

The two pieces user-facing CLI commands actually call:
  - build_or_refresh_index(root, ...) for `hydra-llm index <path>`
  - retrieve(root, query, ...) for `hydra-llm query` and `chat --rag`
"""
from __future__ import annotations

import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from . import (
    config as cfg_mod,
    docker_driver,
    embedding,
    hardware,
    rag_catalog,
    rag_index,
    rag_store,
)


@dataclass
class IndexPlan:
    """What `index` is going to do; computed before any embedding starts."""
    root: Path
    code_embedder: dict | None
    prose_embedder: dict | None
    files_to_embed: list[rag_index.FileInfo] = field(default_factory=list)
    files_unchanged: list[str] = field(default_factory=list)  # rel_paths
    files_deleted: list[str] = field(default_factory=list)
    full_rebuild: bool = False
    walk_summary: rag_index.WalkSummary | None = None
    # True when code and prose use *different* embedders. False (default) means
    # one embedder serves all chunks; the chunks are still tagged by their
    # classifier-determined kind for filtering, but live in one table.
    dual_index: bool = False


@dataclass
class IndexResult:
    plan: IndexPlan
    chunks_added: int = 0
    chunks_removed: int = 0
    code_chunks_total: int = 0
    prose_chunks_total: int = 0
    elapsed_seconds: float = 0.0


def _detect_default_embedders(cfg: dict) -> tuple[dict | None, dict | None]:
    """Pick the default code and prose embedder for this machine.

    Resolution: cfg['rag']['default_code_embedder'] / 'default_prose_embedder'
    if explicitly set, else the first installed embedder of each kind, else
    fall back to recommended-for-tier from the catalog (even if not yet
    downloaded) so the caller can prompt to install.
    """
    catalog, _ = rag_catalog.load_embedder_catalog()
    snap = hardware.system_snapshot()
    tier = hardware.detect_tier(snap)["id"]

    rag_cfg = (cfg.get("rag") or {})
    explicit_code = rag_cfg.get("default_code_embedder")
    explicit_prose = rag_cfg.get("default_prose_embedder")

    def _by_id(eid):
        return next((e for e in catalog if e.get("id") == eid), None)

    def _pick(kind: str):
        # Installed-first preference: an embedder is only useful if it's on
        # disk. Among installed of this kind, prefer ones tagged for our tier.
        installed = [e for e in catalog
                     if e.get("kind") in (kind, "both")
                     and rag_catalog.is_downloaded(e, cfg)]
        for e in installed:
            if tier in (e.get("recommended_for") or []):
                return e
        if installed:
            return installed[0]
        # Nothing installed; fall back to the catalog's tier-recommendation.
        for_tier = [e for e in catalog
                    if e.get("kind") in (kind, "both")
                    and tier in (e.get("recommended_for") or [])]
        return for_tier[0] if for_tier else None

    code = _by_id(explicit_code) or _pick("code")
    prose = _by_id(explicit_prose) or _pick("prose")
    return code, prose


def plan_index(root: Path,
               *,
               cfg: dict | None = None,
               full_rebuild: bool = False,
               extra_excludes: Iterable[str] = (),
               extra_includes: Iterable[str] = (),
               max_depth: int | None = None,
               max_file_size_bytes: int = 1 * 1024 * 1024,
               code_embedder: dict | None = None,
               prose_embedder: dict | None = None,
               dual_index: bool = False,
               only_kind: str | None = None) -> IndexPlan:
    """Compute the plan: which files to (re)embed, which are unchanged,
    which were deleted, and which embedders to use.

    Default mode is single-embedder: every chunk goes through the same
    embedder. Pass dual_index=True to use a code embedder and a prose
    embedder separately and fuse query results via Reciprocal Rank
    Fusion. Dual is the 2026 best practice for very large mixed
    code+prose corpora; for personal-scale folders the quality
    difference is small and not worth the extra disk + RAM.
    """
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    root = Path(root).expanduser().resolve()

    auto_code, auto_prose = _detect_default_embedders(cfg)
    code_e = code_embedder or auto_code
    prose_e = prose_embedder or auto_prose
    if not dual_index:
        # Single-embedder mode: everything goes through the same embedder.
        # Prefer an explicitly-passed one, else the tier's code embedder
        # (handles prose acceptably and is the strongest pick at every
        # tier where one exists).
        chosen = code_embedder or prose_embedder or code_e or prose_e
        code_e = chosen
        prose_e = chosen
    if only_kind == "code":
        prose_e = None
    elif only_kind == "prose":
        code_e = None

    # Walk + classify.
    files, summary = rag_index.walk_folder(
        root,
        max_file_size_bytes=max_file_size_bytes,
        extra_excludes=extra_excludes,
        extra_includes=extra_includes,
        max_depth=max_depth,
    )

    # Filter by what kind of embedder we actually have.
    if code_e is None:
        files = [f for f in files if f.kind != "code"]
    if prose_e is None:
        files = [f for f in files if f.kind != "prose"]

    plan = IndexPlan(
        root=root,
        code_embedder=code_e,
        prose_embedder=prose_e,
        full_rebuild=full_rebuild or not rag_store.has_index(root),
        walk_summary=summary,
        dual_index=dual_index,
    )

    if plan.full_rebuild:
        plan.files_to_embed = files
        return plan

    # Incremental: compare against files.json.
    registry = rag_store.read_files_registry(root)
    by_rel = {f.rel_path: f for f in files}
    seen = set()
    for f in files:
        prev = registry.get(f.rel_path)
        if (prev is None
                or prev.get("size") != f.size
                or prev.get("mtime") != f.mtime):
            plan.files_to_embed.append(f)
        else:
            plan.files_unchanged.append(f.rel_path)
        seen.add(f.rel_path)
    for rel in registry.keys():
        if rel not in seen:
            plan.files_deleted.append(rel)

    # If meta says the index was built with different embedder ids than what
    # we'd use now, force full rebuild -- mixing vectors from different
    # embedders silently destroys retrieval quality.
    meta = rag_store.read_meta(root)
    if meta:
        old_code = meta.get("code_embedder", {}).get("id") if meta.get("code_embedder") else None
        old_prose = meta.get("prose_embedder", {}).get("id") if meta.get("prose_embedder") else None
        new_code = code_e["id"] if code_e else None
        new_prose = prose_e["id"] if prose_e else None
        if old_code != new_code or old_prose != new_prose:
            plan.full_rebuild = True
            plan.files_to_embed = files
            plan.files_unchanged = []
            plan.files_deleted = []

    return plan


def execute_plan(plan: IndexPlan,
                 *,
                 cfg: dict | None = None,
                 batch_size: int = 16,
                 progress=None,
                 register: bool = True,
                 tags: list[str] | None = None) -> IndexResult:
    """Run the plan: ensure embedders are up, chunk + embed changed files,
    delete chunks for vanished/changed files, persist meta + registry.

    progress is an optional callable(stage, info_dict) for live UI updates;
    if None, prints to stderr.
    """
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    started = time.monotonic()
    if progress is None:
        progress = _stderr_progress

    progress("plan", {"plan": plan})

    # Bring embedders up if needed.
    embedders_to_start = [e for e in (plan.code_embedder, plan.prose_embedder) if e]
    # Dedup on id (single-index case shares one embedder).
    seen_ids = set()
    distinct_embedders = []
    for e in embedders_to_start:
        if e["id"] in seen_ids:
            continue
        seen_ids.add(e["id"])
        distinct_embedders.append(e)
    for e in distinct_embedders:
        if not rag_catalog.is_downloaded(e, cfg):
            raise RuntimeError(
                f"embedder {e['id']} is not downloaded. Run "
                f"`hydra-llm rag download {e['id']}` first."
            )
        progress("starting-embedder", {"embedder": e["id"]})
        ok, info = docker_driver.ensure_embedder_running(e, cfg)
        if not ok:
            raise RuntimeError(f"failed to start embedder {e['id']}: "
                               f"{info.get('error')}\n{info.get('logs', '')}")

    # Drop chunks for deleted/changed files.
    files_registry = rag_store.read_files_registry(plan.root) if not plan.full_rebuild else {}
    chunks_removed = 0
    if plan.full_rebuild:
        # Wipe both tables by deleting their dirs; lancedb will recreate.
        idx = rag_store.index_dir(plan.root)
        for sub in (rag_store.CODE_TABLE, rag_store.PROSE_TABLE):
            d = idx / f"{sub}.lance"
            if d.exists():
                import shutil
                shutil.rmtree(d, ignore_errors=True)
        files_registry = {}
    else:
        # For each changed/deleted file, drop its chunks from whichever table
        # it lived in.
        stale_ids_by_kind = {"code": [], "prose": []}
        for rel in plan.files_deleted:
            entry = files_registry.get(rel) or {}
            for cid in entry.get("chunk_ids", []):
                stale_ids_by_kind[entry.get("kind", "prose")].append(cid)
            files_registry.pop(rel, None)
        for f in plan.files_to_embed:
            entry = files_registry.get(f.rel_path) or {}
            for cid in entry.get("chunk_ids", []):
                stale_ids_by_kind[entry.get("kind", f.kind)].append(cid)
        for kind, ids in stale_ids_by_kind.items():
            if ids:
                emb = plan.code_embedder if kind == "code" else plan.prose_embedder
                if emb:  # only delete if the table actually exists for this kind
                    rag_store.delete_by_chunk_ids(plan.root, kind, ids)
                    chunks_removed += len(ids)

    # Embed and write.
    chunks_added = 0
    embedder_for_kind = {"code": plan.code_embedder, "prose": plan.prose_embedder}
    for f in plan.files_to_embed:
        emb = embedder_for_kind.get(f.kind)
        if emb is None:
            continue
        progress("chunking", {"file": f.rel_path})
        chunks = rag_index.chunk_file(f)
        if not chunks:
            files_registry[f.rel_path] = {
                "size": f.size, "mtime": f.mtime, "kind": f.kind, "chunk_ids": [],
            }
            continue
        # Embed in batches.
        rows = []
        chunk_ids = []
        texts = [c.text for c in chunks]
        progress("embedding", {"file": f.rel_path, "chunks": len(chunks),
                               "embedder": emb["id"]})
        vectors = embedding.embed_documents(emb, texts, batch_size=batch_size)
        normalized = embedding.normalize(vectors)
        for c, vec in zip(chunks, normalized):
            cid = rag_store.chunk_id()
            chunk_ids.append(cid)
            rows.append({
                "chunk_id": cid,
                "rel_path": c.rel_path,
                "byte_start": c.byte_start,
                "byte_end": c.byte_end,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "mtime": c.mtime,
                "text": c.text,
                "vector": vec.tolist(),
            })
        rag_store.upsert_chunks(plan.root, f.kind, emb["dimensions"], rows)
        chunks_added += len(rows)
        files_registry[f.rel_path] = {
            "size": f.size, "mtime": f.mtime, "kind": f.kind,
            "chunk_ids": chunk_ids,
        }

    # Persist registry + meta.
    rag_store.write_files_registry(plan.root, files_registry)
    meta = {
        "version": rag_store.CHUNKING_VERSION,
        "created": rag_store.now_iso(),
        "last_updated": rag_store.now_iso(),
        "root": str(plan.root),
        "dual_index": bool(plan.dual_index),
        "code_embedder": (
            {"id": plan.code_embedder["id"], "dimensions": plan.code_embedder["dimensions"]}
            if plan.code_embedder else None
        ),
        "prose_embedder": (
            {"id": plan.prose_embedder["id"], "dimensions": plan.prose_embedder["dimensions"]}
            if plan.prose_embedder else None
        ),
    }
    # Preserve the original created stamp on incremental updates.
    prev_meta = rag_store.read_meta(plan.root)
    if prev_meta.get("created") and not plan.full_rebuild:
        meta["created"] = prev_meta["created"]
    rag_store.write_meta(plan.root, meta)
    if register:
        rag_store.register_store(plan.root, tags=tags)

    return IndexResult(
        plan=plan,
        chunks_added=chunks_added,
        chunks_removed=chunks_removed,
        code_chunks_total=rag_store.chunk_count(plan.root, "code"),
        prose_chunks_total=rag_store.chunk_count(plan.root, "prose"),
        elapsed_seconds=time.monotonic() - started,
    )


def _stderr_progress(stage: str, info: dict):
    """Default progress reporter: human-readable lines on stderr."""
    if stage == "plan":
        plan = info["plan"]
        s = plan.walk_summary
        sys.stderr.write(
            f"  plan: {len(plan.files_to_embed)} to embed, "
            f"{len(plan.files_unchanged)} unchanged, "
            f"{len(plan.files_deleted)} removed; "
            f"walked {s.files_total} files, kept {s.files_kept} "
            f"(code {s.code_count}, prose {s.prose_count})\n"
        )
    elif stage == "starting-embedder":
        sys.stderr.write(f"  starting embedder: {info['embedder']}\n")
    elif stage == "embedding":
        sys.stderr.write(
            f"  embedding {info['chunks']:>4} chunks  {info['file']:<60}  "
            f"({info['embedder']})\n"
        )


# --- retrieval ----------------------------------------------------------------

def retrieve(root: Path,
             query: str,
             *,
             top_k: int = 5,
             cfg: dict | None = None,
             code_only: bool = False,
             prose_only: bool = False,
             rrf_k: int = 60) -> list[dict]:
    """Embed `query` with both indexed embedders, search both tables,
    fuse with Reciprocal Rank Fusion. Returns list of result dicts ordered
    by fused score (highest first).
    """
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    root = Path(root).expanduser().resolve()
    if not rag_store.has_index(root):
        raise RuntimeError(f"no index at {root} (no .hydra-index/). "
                           f"Run `hydra-llm index {root}` first.")
    meta = rag_store.read_meta(root)
    code_meta = meta.get("code_embedder")
    prose_meta = meta.get("prose_embedder")
    catalog, _ = rag_catalog.load_embedder_catalog()
    by_id = {e["id"]: e for e in catalog}

    code_e = by_id.get((code_meta or {}).get("id")) if code_meta else None
    prose_e = by_id.get((prose_meta or {}).get("id")) if prose_meta else None

    if code_only:
        prose_e = None
    if prose_only:
        code_e = None
    if code_e is None and prose_e is None:
        raise RuntimeError(
            "neither code nor prose embedder is available for this index. "
            "The embedder catalog may have changed since indexing."
        )

    # Bring embedders up.
    distinct = []
    seen = set()
    for e in (code_e, prose_e):
        if e and e["id"] not in seen:
            seen.add(e["id"])
            distinct.append(e)
    for e in distinct:
        if not rag_catalog.is_downloaded(e, cfg):
            raise RuntimeError(
                f"embedder {e['id']} is not downloaded. Run "
                f"`hydra-llm rag download {e['id']}` first."
            )
        ok, info = docker_driver.ensure_embedder_running(e, cfg)
        if not ok:
            raise RuntimeError(f"failed to start embedder {e['id']}: "
                               f"{info.get('error')}")

    per_kind: dict[str, list[dict]] = {}
    if code_e:
        qv = embedding.normalize(embedding.embed_query(code_e, query))
        per_kind["code"] = rag_store.search(root, "code", qv.tolist(), top_k=top_k * 3)
    if prose_e:
        qv = embedding.normalize(embedding.embed_query(prose_e, query))
        per_kind["prose"] = rag_store.search(root, "prose", qv.tolist(), top_k=top_k * 3)

    # Reciprocal Rank Fusion. score = sum over rankers of 1/(rrf_k + rank).
    fused: dict[str, dict] = {}
    for kind, hits in per_kind.items():
        for rank, h in enumerate(hits, start=1):
            cid = h.get("chunk_id")
            if not cid:
                continue
            entry = fused.get(cid)
            if entry is None:
                entry = {**h, "kinds": [], "ranks": {}, "rrf": 0.0}
                fused[cid] = entry
            entry["kinds"].append(kind)
            entry["ranks"][kind] = rank
            entry["rrf"] += 1.0 / (rrf_k + rank)

    ordered = sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)
    return ordered[:top_k]


def retrieve_federated(query: str,
                       *,
                       store_paths: list[str] | None = None,
                       tags: list[str] | None = None,
                       top_k: int = 5,
                       cfg: dict | None = None,
                       code_only: bool = False,
                       prose_only: bool = False,
                       rrf_k: int = 60) -> list[dict]:
    """Cross-folder retrieval: union of stores filtered by paths/tags, RRF
    fusion across all per-store hits.

    If neither `store_paths` nor `tags` is given, queries every registered
    store. Each result dict includes `store_path` so the caller can render
    "which folder did this come from".
    """
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    matching = rag_store.stores_matching(paths_filter=store_paths, tags_filter=tags)
    if not matching:
        if store_paths or tags:
            raise RuntimeError("no stores match the given filter")
        raise RuntimeError(
            "no indexed folders. Run `hydra-llm index <path>` somewhere first."
        )

    # Pre-flight: which embedders do we need? Bring them up once, share
    # across stores. If a store's embedder isn't installed, skip the store
    # with a soft warning rather than aborting the whole query.
    needed_embedder_ids = set()
    catalog, _ = rag_catalog.load_embedder_catalog()
    cat_by_id = {e["id"]: e for e in catalog}
    plans = []  # (store_path, code_e, prose_e, code_meta, prose_meta)
    skipped: list[tuple[str, str]] = []
    for s in matching:
        rp = Path(s["path"])
        if not (rp / rag_store.INDEX_DIR_NAME).is_dir():
            skipped.append((str(rp), "missing-index-dir"))
            continue
        meta = rag_store.read_meta(rp)
        ce_id = (meta.get("code_embedder") or {}).get("id")
        pe_id = (meta.get("prose_embedder") or {}).get("id")
        ce = cat_by_id.get(ce_id) if ce_id else None
        pe = cat_by_id.get(pe_id) if pe_id else None
        if code_only:
            pe = None
        if prose_only:
            ce = None
        if ce is None and pe is None:
            skipped.append((str(rp), "no-embedder-for-mode"))
            continue
        # Drop the embedder if its GGUF isn't on disk.
        if ce and not rag_catalog.is_downloaded(ce, cfg):
            skipped.append((str(rp), f"embedder-missing:{ce['id']}"))
            ce = None
        if pe and not rag_catalog.is_downloaded(pe, cfg):
            if ce is None:
                skipped.append((str(rp), f"embedder-missing:{pe['id']}"))
                continue
            pe = None
        if ce is None and pe is None:
            continue
        if ce:
            needed_embedder_ids.add(ce["id"])
        if pe:
            needed_embedder_ids.add(pe["id"])
        plans.append((str(rp), ce, pe))

    if not plans:
        msg = "no usable stores found"
        if skipped:
            details = "; ".join(f"{p}: {why}" for p, why in skipped[:5])
            msg += f" ({details})"
        raise RuntimeError(msg)

    # Bring up each distinct embedder once.
    for eid in needed_embedder_ids:
        e = cat_by_id[eid]
        ok, info = docker_driver.ensure_embedder_running(e, cfg)
        if not ok:
            raise RuntimeError(f"failed to start embedder {eid}: "
                               f"{info.get('error')}")

    # Embed the query once per distinct embedder; reuse across stores.
    query_vecs: dict[str, list[float]] = {}
    for eid in needed_embedder_ids:
        e = cat_by_id[eid]
        v = embedding.normalize(embedding.embed_query(e, query))
        query_vecs[eid] = v.tolist()

    # Search each store/kind, collect hits with their store path attached.
    fused: dict[str, dict] = {}
    for store_path, ce, pe in plans:
        rp = Path(store_path)
        per_kind = {}
        if ce:
            per_kind["code"] = rag_store.search(rp, "code", query_vecs[ce["id"]],
                                                top_k=top_k * 3)
        if pe:
            per_kind["prose"] = rag_store.search(rp, "prose", query_vecs[pe["id"]],
                                                 top_k=top_k * 3)
        for kind, hits in per_kind.items():
            for rank, h in enumerate(hits, start=1):
                cid = h.get("chunk_id")
                if not cid:
                    continue
                # Compose a globally-unique fused-id so two stores' chunks
                # with the same uuid (theoretically possible) don't merge.
                fid = f"{store_path}|{cid}"
                entry = fused.get(fid)
                if entry is None:
                    entry = {**h, "store_path": store_path,
                             "kinds": [], "ranks": {}, "rrf": 0.0}
                    fused[fid] = entry
                entry["kinds"].append(kind)
                entry["ranks"][kind] = rank
                entry["rrf"] += 1.0 / (rrf_k + rank)

    ordered = sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)
    return ordered[:top_k]
