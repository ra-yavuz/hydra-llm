"""Idle-TTL reaper for chat-model containers.

Mirrors the embedder reaper in `embedding.py` for chat-model containers.
Touch files at `paths.MODEL_TOUCH_DIR/<alias>` carry the mtime of the last
observed activity; `reap_idle_models()` stops containers whose touch is
older than `cfg['chat_idle_ttl_seconds']`.

Activity sources:
1. `hydra-llm chat` calls `touch_model(alias)` after every assistant turn
   and once at start time so a freshly-launched model gets a full TTL of
   grace before reaping kicks in.
2. The reaper itself samples `docker stats` for each running container.
   Any container above `cfg['reap_cpu_busy_percent']` (default 1%) is
   touched on the spot, so external clients (Aider, curl, the Plasma
   widget, lillycoder, etc.) keep their models alive purely by using
   them.

The reaper is meant to run from a low-frequency user-level systemd timer
(see scripts/reaper-unit.sh and `hydra-llm reaper enable`). One cycle is
cheap: a `docker ps`, a `docker stats --no-stream`, a few stat() calls,
and at most one `docker rm -f` per idle alias.
"""
from __future__ import annotations

import os
import subprocess
import time

from . import config as cfg_mod, docker_driver, paths


def _touch_path(alias: str):
    return paths.MODEL_TOUCH_DIR / alias


def touch_model(alias: str) -> None:
    """Mark the chat model `alias` as just-used. Idempotent and fail-soft."""
    if not alias:
        return
    p = _touch_path(alias)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.touch()
        else:
            os.utime(p, None)
    except OSError:
        pass


def last_touch_seconds_ago(alias: str) -> float | None:
    p = _touch_path(alias)
    try:
        return time.time() - p.stat().st_mtime
    except (OSError, FileNotFoundError):
        return None


def _sample_cpu_percent(container_names: list[str]) -> dict[str, float]:
    """Return a {container_name: cpu_percent} snapshot for the given containers.

    `docker stats --no-stream` does one read of the cgroup CPU counter and
    returns immediately. Output looks like `12.34%` per row. Failures
    (docker missing, container vanished mid-call) are swallowed and the
    affected entries are simply absent from the result.
    """
    if not container_names:
        return {}
    try:
        r = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}", *container_names],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if r.returncode != 0:
        return {}
    out: dict[str, float] = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, cpu = parts[0].strip(), parts[1].strip().rstrip("%")
        try:
            out[name] = float(cpu)
        except ValueError:
            continue
    return out


def reap_idle_models(cfg: dict | None = None) -> list[str]:
    """Stop chat-model containers idle longer than chat_idle_ttl_seconds.

    Fail-soft: returns [] on any unexpected error. Returns the list of
    aliases that were actually torn down this cycle.
    """
    try:
        if cfg is None:
            cfg = cfg_mod.load_user_config()
        ttl = int(cfg.get("chat_idle_ttl_seconds") or 0)
        if ttl <= 0:
            return []
        rows, err = docker_driver.list_running(cfg)
        if err or not rows:
            return []
        running = [r for r in rows if r.get("state") == "running"]
        if not running:
            return []

        # 1. Sample CPU% so any externally-driven activity refreshes the
        #    touch before we check ages.
        busy_threshold = float(cfg.get("reap_cpu_busy_percent") or 0.0)
        cpu = _sample_cpu_percent([r["container"] for r in running])
        for r in running:
            pct = cpu.get(r["container"])
            if pct is not None and pct > busy_threshold:
                touch_model(r["alias"])

        # 2. Reap aliases whose last touch is older than the TTL.
        stopped: list[str] = []
        for r in running:
            alias = r["alias"]
            ago = last_touch_seconds_ago(alias)
            # No touch on record yet (legacy container, started before
            # the reaper module existed): seed it now and let one full
            # TTL elapse before we kill it.
            if ago is None:
                touch_model(alias)
                continue
            if ago > ttl:
                ok, _info = docker_driver.stop(alias, cfg)
                if ok:
                    stopped.append(alias)
        return stopped
    except Exception:
        return []
