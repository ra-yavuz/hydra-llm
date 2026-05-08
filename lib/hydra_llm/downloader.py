"""GGUF downloader. Anonymous HTTP, optional HF_TOKEN passthrough."""
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths


def download(catalog_entry, cfg, force: bool = False, on_progress=None,
             dest_dir: Path | None = None) -> Path:
    """Download the GGUF for catalog_entry into dest_dir, defaulting to
    cfg['models_dir']. Returns the destination Path. Raises on error.

    on_progress(downloaded_bytes, total_bytes) called periodically; if None,
    prints to stderr. Pass dest_dir to direct the download somewhere other
    than the chat-models directory (e.g. the embedders dir for RAG).
    """
    url = catalog_entry["url"]
    filename = catalog_entry["filename"]
    if dest_dir is not None:
        target_dir = Path(dest_dir).expanduser()
    else:
        target_dir = Path(cfg.get("models_dir") or paths.MODELS_DIR_DEFAULT).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename

    if dest.exists() and not force:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "hydra-llm/0.1"}
    token = os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in url:
        headers["Authorization"] = f"Bearer {token}"

    # Resume support: if a partial download exists from a previous attempt,
    # ask the server for the rest with Range. If the server doesn't honor
    # ranges, we restart from zero (handled below by checking the response
    # status code).
    resume_from = 0
    if tmp.exists() and not force:
        try:
            resume_from = tmp.stat().st_size
        except OSError:
            resume_from = 0
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    req = urllib.request.Request(url, headers=headers)
    open_mode = "ab" if resume_from > 0 else "wb"
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        # If the server refuses the Range (416 Requested Range Not Satisfiable),
        # the .part is probably already complete or corrupt. Drop it and start
        # over with no Range header.
        if e.code in (416,) and resume_from > 0:
            try:
                tmp.unlink()
            except OSError:
                pass
            resume_from = 0
            headers.pop("Range", None)
            req = urllib.request.Request(url, headers=headers)
            try:
                resp = urllib.request.urlopen(req)
            except urllib.error.HTTPError as e2:
                raise RuntimeError(f"download failed: HTTP {e2.code} {e2.reason}") from e2
            except urllib.error.URLError as e2:
                raise RuntimeError(f"download failed: {e2.reason}") from e2
            open_mode = "wb"
        else:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise RuntimeError(f"download failed: HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(f"download failed: {e.reason}") from e

    # If we asked for a Range but got 200 (full body), the server didn't
    # honor it; truncate and start from zero.
    if resume_from > 0 and resp.status == 200:
        resp.close()
        try:
            tmp.unlink()
        except OSError:
            pass
        resume_from = 0
        headers.pop("Range", None)
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise RuntimeError(f"download failed: {e}") from e
        open_mode = "wb"

    try:
        with resp, open(tmp, open_mode) as out:
            content_len = int(resp.headers.get("Content-Length", 0))
            total = content_len + resume_from
            chunk = 1 << 16  # 64 KiB
            done = resume_from
            last_report = 0.0
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                now = time.time()
                if on_progress:
                    on_progress(done, total)
                elif now - last_report >= 0.5:
                    _print_progress(done, total)
                    last_report = now
            if not on_progress:
                _print_progress(done, total, final=True)
    except urllib.error.URLError as e:
        # Network drop mid-stream. Leave the .part on disk so the next
        # invocation resumes from where we stopped.
        raise RuntimeError(f"download interrupted: {e.reason}. "
                           f"Re-run to resume from {tmp.stat().st_size} bytes") from e

    expected = catalog_entry.get("sha256")
    if expected:
        actual = sha256_of_file(tmp)
        if actual.lower() != expected.lower():
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"sha256 mismatch: expected {expected}, got {actual}"
            )

    tmp.rename(dest)
    return dest


def sha256_of_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _print_progress(done: int, total: int, final: bool = False):
    if total > 0:
        pct = 100.0 * done / total
        msg = f"  downloaded {done / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MiB ({pct:.1f}%)"
    else:
        msg = f"  downloaded {done / 1024 / 1024:.1f} MiB"
    end = "\n" if final else "\r"
    sys.stderr.write("\r" + " " * 80 + "\r" + msg + end)
    sys.stderr.flush()


def remove_local(catalog_entry, cfg) -> bool:
    """Delete the GGUF for this entry. Returns True if a file was removed."""
    models_dir = Path(cfg.get("models_dir") or paths.MODELS_DIR_DEFAULT).expanduser()
    p = models_dir / catalog_entry["filename"]
    if p.exists():
        p.unlink()
        return True
    return False


def is_downloaded(catalog_entry, cfg) -> bool:
    models_dir = Path(cfg.get("models_dir") or paths.MODELS_DIR_DEFAULT).expanduser()
    return (models_dir / catalog_entry["filename"]).exists()
