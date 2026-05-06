"""User-level systemd autostart for a hydra-llm model.

A user unit (~/.config/systemd/user/hydra-llm-autostart.service) is written
on demand by `hydra-llm autostart <id>`. It runs `hydra-llm start <id>` when
the user logs in and the user systemd manager comes up, so no root is
needed and Docker runs as the user.

Disable with `hydra-llm autostart --off`. The unit file is removed.

Notes:
* We don't ship this unit in the .deb because it embeds a model id, which
  is per-user state.
* `linger` is intentionally not enabled here; that would start the model
  at boot before login. If a user wants that, they can run
  `loginctl enable-linger` themselves and we'll honor it via WantedBy.
"""
import os
import shutil
import subprocess
from pathlib import Path

UNIT_NAME = "hydra-llm-autostart.service"


def _unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _unit_path() -> Path:
    return _unit_dir() / UNIT_NAME


def _hydra_bin() -> str:
    """Pick the hydra-llm binary the unit should call. Prefer an installed
    /usr/bin/hydra-llm; fall back to whatever's on PATH so dev installs work."""
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
    """Return current autostart state. No side effects.

    Returns a dict: {enabled: bool, model: str|None, unit_path: str,
                     active: bool|None, error: str|None}
    """
    out = {
        "enabled": False,
        "model": None,
        "unit_path": str(_unit_path()),
        "active": None,
        "error": None,
    }
    p = _unit_path()
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("Environment=HYDRA_LLM_AUTOSTART_MODEL="):
                out["model"] = line.split("=", 2)[2]
                break
        try:
            r = _systemctl_user("is-enabled", UNIT_NAME)
            out["enabled"] = (r.stdout.strip() == "enabled")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            out["error"] = f"systemctl unavailable: {e}"
        try:
            r = _systemctl_user("is-active", UNIT_NAME)
            out["active"] = (r.stdout.strip() == "active")
        except Exception:
            pass
    return out


def enable(model_id: str) -> tuple[bool, str]:
    """Write the unit and `systemctl --user enable` it. Returns (ok, message)."""
    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = _unit_path()
    bin_path = _hydra_bin()

    # The unit just shells out to `hydra-llm start <id>`. RemainAfterExit so
    # systemd treats it as "active" once start returns 0; the actual server
    # is a docker container managed independently.
    contents = f"""[Unit]
Description=hydra-llm: autostart model {model_id}
After=default.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=HYDRA_LLM_AUTOSTART_MODEL={model_id}
ExecStart={bin_path} start {model_id}
ExecStop={bin_path} stop {model_id}

[Install]
WantedBy=default.target
"""
    unit_path.write_text(contents)

    r = _systemctl_user("daemon-reload")
    if r.returncode != 0:
        return False, f"systemctl --user daemon-reload failed: {r.stderr.strip()}"
    r = _systemctl_user("enable", UNIT_NAME)
    if r.returncode != 0:
        return False, f"systemctl --user enable failed: {r.stderr.strip()}"
    return True, f"autostart enabled for {model_id}\n  unit: {unit_path}"


def disable() -> tuple[bool, str]:
    """Disable and remove the unit. Returns (ok, message). Idempotent."""
    unit_path = _unit_path()
    if not unit_path.is_file():
        return True, "autostart already off (no unit installed)."
    # Best effort: disable, ignore errors so we still remove the file.
    _systemctl_user("disable", UNIT_NAME)
    _systemctl_user("stop", UNIT_NAME)
    unit_path.unlink()
    _systemctl_user("daemon-reload")
    return True, "autostart disabled."
