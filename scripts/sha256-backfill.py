#!/usr/bin/env python3
"""Compute and write sha256 fields into the shipped catalogs.

Walks catalog/catalog.yaml and catalog/embedders.yaml and writes a
sha256 next to every `url:` that doesn't have one yet. Idempotent:
entries already populated are skipped unless `--force` is given.

Two modes for getting the hash:

  HF API (default, fast, no bandwidth)
    For URLs of the form
    `https://huggingface.co/<owner>/<repo>/resolve/<ref>/<filename>`,
    fetch `https://huggingface.co/api/models/<owner>/<repo>/tree/<ref>`
    and take the `lfs.oid` of the matching path. HF's LFS oid is
    sha256 by spec, so this returns the exact hash hydra needs without
    downloading the GGUF.

  Streaming download (fallback, opt-in via --full-download)
    For URLs the HF API can't answer (mirror redirects, GitHub release
    artifacts, etc.), stream the file once and hash on the fly. Slow
    on a typical home connection.

Why this exists: the downloader already verifies sha256 if the catalog
provides one (lib/hydra_llm/downloader.py). Without that field, a
corrupt/incomplete download silently produces a broken local model. This
script populates the field once so future downloads fail loudly.

Usage:
    scripts/sha256-backfill.py                  # HF API for HF URLs, skip the rest
    scripts/sha256-backfill.py --force          # recompute even if present
    scripts/sha256-backfill.py --only catalog   # just chat catalog
    scripts/sha256-backfill.py --only embedders
    scripts/sha256-backfill.py --dry-run        # print, don't write
    scripts/sha256-backfill.py --full-download  # also stream-hash non-HF URLs
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHAT_CATALOG = REPO / "catalog" / "catalog.yaml"
EMBED_CATALOG = REPO / "catalog" / "embedders.yaml"


_HF_URL = re.compile(
    r"^https://huggingface\.co/(?P<repo>[^/]+/[^/]+)/resolve/(?P<ref>[^/]+)/(?P<path>.+)$"
)


def hf_lookup_sha256(url: str, *, cache: dict[str, dict] | None = None) -> str | None:
    """Resolve sha256 for a Hugging Face `resolve/<ref>` URL via the tree API.

    Returns the sha256 hex string, or None if the URL isn't a recognizable
    HF resolve URL or the file isn't LFS-stored (rare for GGUFs but
    possible for tiny configs).

    `cache` is an optional dict keyed on (repo, ref) to avoid re-fetching
    the same tree for every quant variant of the same repo.
    """
    m = _HF_URL.match(url)
    if not m:
        return None
    repo = m.group("repo")
    ref = m.group("ref")
    path = m.group("path")
    key = f"{repo}@{ref}"
    if cache is not None and key in cache:
        tree = cache[key]
    else:
        api = f"https://huggingface.co/api/models/{repo}/tree/{ref}?recursive=true"
        headers = {"User-Agent": "hydra-llm-sha-backfill/2"}
        token = os.environ.get("HF_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(api, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                data = resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError):
            return None
        try:
            entries = __import__("json").loads(data.decode("utf-8"))
        except ValueError:
            return None
        # Index by path so multiple quant variants in the same repo only
        # cost one API call.
        tree = {e.get("path"): e for e in entries if isinstance(e, dict)}
        if cache is not None:
            cache[key] = tree
    entry = tree.get(path)
    if not entry:
        return None
    lfs = entry.get("lfs") or {}
    oid = lfs.get("oid")
    # HF's lfs.oid is sha256 hex by Git LFS spec. Sanity-check the shape.
    if isinstance(oid, str) and len(oid) == 64 and all(c in "0123456789abcdef" for c in oid.lower()):
        return oid.lower()
    return None


def stream_sha256(url: str, *, chunk: int = 1 << 20) -> tuple[str, int]:
    """Download `url` once, hashing as we go. Returns (hex, bytes_total).

    Anonymous request; uses HF_TOKEN if present and url is on
    huggingface.co (matches the runtime downloader's behavior).
    """
    headers = {"User-Agent": "hydra-llm-sha-backfill/1"}
    token = os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    h = hashlib.sha256()
    total = 0
    last_pct = -1
    with urllib.request.urlopen(req) as resp:
        size_hint = int(resp.headers.get("Content-Length") or 0)
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            h.update(buf)
            total += len(buf)
            if size_hint:
                pct = int(100 * total / size_hint)
                if pct != last_pct and pct % 5 == 0:
                    sys.stderr.write(f"\r    {pct:>3}% ({total / 1024 / 1024:.0f} MiB)")
                    sys.stderr.flush()
                    last_pct = pct
        sys.stderr.write("\r" + " " * 60 + "\r")
    return h.hexdigest(), total


def patch_yaml_inplace(yaml_path: Path, updates: dict[str, str], dry_run: bool) -> int:
    """Insert/replace `sha256:` lines next to matching `url:` lines.

    `updates` maps url -> sha256 hex. We patch by string-matching the URL
    so we preserve the existing YAML's comments and ordering (PyYAML
    doesn't round-trip comments). Returns the number of lines changed.
    """
    text = yaml_path.read_text()
    out_lines: list[str] = []
    changed = 0
    pending_url: str | None = None
    pending_indent = ""
    for line in text.splitlines(keepends=True):
        m = re.match(r"^(\s*)url:\s*(\S.*)$", line.rstrip("\n"))
        if m:
            pending_indent = m.group(1)
            pending_url = m.group(2).strip().strip('"').strip("'")
            out_lines.append(line)
            continue
        if pending_url is not None:
            sha_for_url = updates.get(pending_url)
            existing = re.match(r"^(\s*)sha256:\s*(\S+)\s*$", line.rstrip("\n"))
            if existing and sha_for_url and existing.group(2) != sha_for_url:
                out_lines.append(f"{pending_indent}sha256: {sha_for_url}\n")
                changed += 1
                pending_url = None
                continue
            if existing:
                out_lines.append(line)
                pending_url = None
                continue
            if sha_for_url:
                out_lines.append(f"{pending_indent}sha256: {sha_for_url}\n")
                changed += 1
            pending_url = None
        out_lines.append(line)
    if changed and not dry_run:
        yaml_path.write_text("".join(out_lines))
    return changed


def collect_url_to_sha(yaml_path: Path) -> dict[str, str | None]:
    """Cheap parse: pair each `url:` with the next `sha256:` if present."""
    out: dict[str, str | None] = {}
    pending: str | None = None
    for line in yaml_path.read_text().splitlines():
        m_url = re.match(r"^\s*url:\s*(\S.*)$", line)
        if m_url:
            if pending and pending not in out:
                out[pending] = None
            pending = m_url.group(1).strip().strip('"').strip("'")
            continue
        if pending is None:
            continue
        m_sha = re.match(r"^\s*sha256:\s*(\S+)\s*$", line)
        if m_sha:
            out[pending] = m_sha.group(1)
            pending = None
    if pending and pending not in out:
        out[pending] = None
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true",
                    help="recompute even when a sha256 is already present")
    ap.add_argument("--only", choices=("catalog", "embedders"),
                    help="restrict to one catalog file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change without writing the YAML")
    ap.add_argument("--full-download", action="store_true",
                    help="also stream-hash URLs the HF API can't answer "
                         "(slow; uses your bandwidth). Without this, non-HF "
                         "URLs are listed as skipped and left without sha256.")
    args = ap.parse_args()

    targets: list[Path] = []
    if args.only == "catalog":
        targets = [CHAT_CATALOG]
    elif args.only == "embedders":
        targets = [EMBED_CATALOG]
    else:
        targets = [CHAT_CATALOG, EMBED_CATALOG]

    rc = 0
    for path in targets:
        if not path.is_file():
            print(f"[skip] {path} (missing)")
            continue
        print(f"[scan] {path}")
        url_to_sha = collect_url_to_sha(path)
        todo = [u for u, sha in url_to_sha.items()
                if u and (sha is None or args.force)]
        if not todo:
            print(f"  nothing to do ({len(url_to_sha)} entries already populated)")
            continue
        updates: dict[str, str] = {}
        hf_cache: dict[str, dict] = {}
        for i, url in enumerate(todo, 1):
            print(f"  [{i}/{len(todo)}] {url[:90]}{'...' if len(url) > 90 else ''}")
            sha = hf_lookup_sha256(url, cache=hf_cache)
            if sha:
                print(f"    {sha}  (via HF API)")
                updates[url] = sha
                continue
            if not args.full_download:
                print("    SKIPPED: HF API didn't resolve, --full-download not given")
                continue
            try:
                sha, size = stream_sha256(url)
            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                print(f"    FAILED: {e}", file=sys.stderr)
                rc = 1
                continue
            print(f"    {sha}  ({size / 1024 / 1024:.0f} MiB, streamed)")
            updates[url] = sha
        if updates:
            n = patch_yaml_inplace(path, updates, args.dry_run)
            verb = "would update" if args.dry_run else "updated"
            print(f"  {verb} {n} sha256 entries")
    return rc


if __name__ == "__main__":
    sys.exit(main())
