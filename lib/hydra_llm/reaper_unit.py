"""User-level systemd timer that runs the chat-model autokill reaper.

`hydra-llm reaper enable` writes a pair of unit files into
`~/.config/systemd/user/`:

  hydra-llm-reaper.service   oneshot, runs `hydra-llm reap`
  hydra-llm-reaper.timer     fires every 60s while the user session is up

The timer wakes the service from systemd; no long-lived hydra process is
held in memory between runs. Each cycle is cheap (one `docker ps`, one
`docker stats --no-stream`, a few stat() calls), so CPU cost is dominated
by docker itself and stays a small fraction of one core for a couple
hundred milliseconds per minute.

`hydra-llm reaper disable` removes both unit files. `loginctl
enable-linger` is requested best-effort so the timer keeps running across
logout (so an SSH session that left a model up still gets reaped).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SERVICE_NAME = "hydra-llm-reaper.service"
TIMER_NAME = "hydra-llm-reaper.timer"


def _unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _service_path() -> Path:
    return _unit_dir() / SERVICE_NAME


def _timer_path() -> Path:
    return _unit_dir() / TIMER_NAME


def _hydra_bin() -> str:
    for cand in ("/usr/bin/hydra-llm", "/usr/local/bin/hydra-llm"):
        if Path(cand).is_file():
            return cand
    found = shutil.which("hydra-llm")
    return found or "/usr/bin/hydra-llm"


def _systemctl_user(*args, check=False):
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True, check=check, timeout=10,
    )


def status() -> dict:
    """Report current reaper state. Pure read; no side effects."""
    out = {
        "installed": False,
        "enabled": False,
        "active": None,
        "service_path": str(_service_path()),
        "timer_path": str(_timer_path()),
        "error": None,
    }
    if _service_path().is_file() and _timer_path().is_file():
        out["installed"] = True
    try:
        r = _systemctl_user("is-enabled", TIMER_NAME)
        out["enabled"] = (r.stdout.strip() == "enabled")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        out["error"] = f"systemctl unavailable: {e}"
    try:
        r = _systemctl_user("is-active", TIMER_NAME)
        out["active"] = (r.stdout.strip() == "active")
    except Exception:
        pass
    return out


def enable(interval_seconds: int = 60) -> tuple[bool, str]:
    """Install and start the reaper timer. Idempotent.

    The default 60s tick is fine: the reaper itself is cheap, and a
    finer cadence would just be docker-stats overhead. A coarser cadence
    delays the autokill by up to one tick, which is acceptable.
    """
    if interval_seconds < 10:
        interval_seconds = 10
    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    bin_path = _hydra_bin()

    service = f"""[Unit]
Description=hydra-llm chat-model idle-TTL reaper
After=default.target

[Service]
Type=oneshot
ExecStart={bin_path} reap

[Install]
WantedBy=default.target
"""
    timer = f"""[Unit]
Description=hydra-llm reaper tick

[Timer]
OnBootSec=30s
OnUnitActiveSec={interval_seconds}s
AccuracySec=15s
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""
    _service_path().write_text(service)
    _timer_path().write_text(timer)

    r = _systemctl_user("daemon-reload")
    if r.returncode != 0:
        return False, f"systemctl --user daemon-reload failed: {r.stderr.strip()}"
    r = _systemctl_user("enable", "--now", TIMER_NAME)
    if r.returncode != 0:
        return False, f"systemctl --user enable --now {TIMER_NAME} failed: {r.stderr.strip()}"
    # Best-effort linger so the reaper keeps ticking after the user logs
    # out (SSH session that started a model and disconnected).
    try:
        subprocess.run(["loginctl", "enable-linger"],
                       capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return True, (f"reaper enabled. Timer ticks every {interval_seconds}s.\n"
                  f"  service: {_service_path()}\n"
                  f"  timer:   {_timer_path()}")


def disable() -> tuple[bool, str]:
    """Stop and remove the reaper timer + service. Idempotent."""
    sp, tp = _service_path(), _timer_path()
    if not sp.is_file() and not tp.is_file():
        return True, "reaper already off (no unit installed)."
    _systemctl_user("disable", "--now", TIMER_NAME)
    _systemctl_user("stop", SERVICE_NAME)
    for p in (sp, tp):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    _systemctl_user("daemon-reload")
    return True, "reaper disabled."
