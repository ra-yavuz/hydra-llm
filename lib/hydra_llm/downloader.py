"""GGUF downloader. Anonymous HTTP, optional HF_TOKEN passthrough."""
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths


def download(catalog_entry, cfg, force: bool = False, on_progress=None) -> Path:
    """Download the GGUF for catalog_entry into cfg['models_dir'].
    Returns the destination Path. Raises on error.

    on_progress(downloaded_bytes, total_bytes) called periodically; if None, prints to stderr.
    """
    url = catalog_entry["url"]
    filename = catalog_entry["filename"]
    models_dir = Path(cfg.get("models_dir") or paths.MODELS_DIR_DEFAULT).expanduser()
    models_dir.mkdir(parents=True, exist_ok=True)
    dest = models_dir / filename

    if dest.exists() and not force:
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "hydra-llm/0.1"}
    token = os.environ.get("HF_TOKEN")
    if token and "huggingface.co" in url:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            chunk = 1 << 16  # 64 KiB
            done = 0
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
    except urllib.error.HTTPError as e:
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
