"""Tray-flavored helpers used by the Plasmoid (and any other GUI integration).

The Plasmoid shells out to `hydra-llm tray <subcommand>` and parses JSON.
Everything here is a thin wrapper over the existing modules; the only reason
this module exists is to keep tray-specific output shape out of the user-facing
CLI commands (status, list, etc.) so we can evolve the GUI without breaking
human-readable command output.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as cfg_mod, docker_driver, downloader, hardware, overrides, paths


CONTAINER_PREFIX = "hydra-"  # matches docker_driver default


def _system_summary():
    snap = hardware.system_snapshot()
    cpu_pct = hardware.cpu_pct(0.1)
    ram = snap["ram"]
    gpus = snap["gpus"]
    vram_used = sum(g.get("vram_used_mb", 0) for g in gpus)
    vram_total = sum(g.get("vram_total_mb", 0) for g in gpus)
    return {
        "cpu_pct": cpu_pct,
        "ram_used_mb": ram["used_mb"],
        "ram_total_mb": ram["total_mb"],
        "ram_pct": int(round(100 * ram["used_mb"] / ram["total_mb"])) if ram["total_mb"] else 0,
        "gpu_pct": max((g["util_pct"] for g in gpus), default=0),
        "vram_used_mb": vram_used,
        "vram_total_mb": vram_total,
        "vram_pct": int(round(100 * vram_used / vram_total)) if vram_total else 0,
        "gpus": gpus,
    }


def cmd_status():
    """Tray status: configured models + running + system summary, in one shot."""
    cfg = cfg_mod.load_user_config()
    catalog, _ = cfg_mod.load_catalog()
    running, derr = docker_driver.list_running(cfg)
    docker_driver.annotate_health(running)
    by_alias = {r["alias"]: r for r in running}

    snap = hardware.system_snapshot()
    models = []
    for m in catalog:
        alias = m["id"]
        r = by_alias.get(alias)
        downloaded = downloader.is_downloaded(m, cfg)
        fits, _why = hardware.fits_locally(m, snap)
        models.append({
            "alias": alias,
            "name": m.get("name", alias),
            "size_gb": m.get("size_gb"),
            "downloaded": downloaded,
            "running": r is not None,
            "ready": bool(r and r.get("ready")),
            "running_port": r["port"] if r else None,
            "container": r["container"] if r else None,
            "status": r["status"] if r else None,
            "fit": fits,
            "recommended_for": m.get("recommended_for", []),
            "rag_index": m.get("rag_index"),
        })

    extras = [r for r in running if r["alias"] not in {m["id"] for m in catalog}]

    print(json.dumps({
        "ok": True,
        "config_path": str(paths.CONFIG_DIR),
        "models": models,
        "extra_running": extras,
        "summary": _system_summary(),
        "docker_error": derr,
    }))
    return 0


def cmd_logs(alias: str, tail: int = 200):
    """Recent container logs as JSON {ok, lines, missing}.

    `missing: true` distinguishes a crashed/removed container from a generic
    docker error so the GUI can clear pending markers.
    """
    cfg = cfg_mod.load_user_config()
    name = f"{cfg['container_prefix']}{alias}"
    try:
        out = subprocess.run(
            ["docker", "logs", "--tail", str(tail), name],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            err = out.stderr.strip() or "container not found"
            missing = "No such container" in err or "no such container" in err.lower()
            print(json.dumps({"ok": False, "error": err, "missing": missing}))
            return 1
        merged = (out.stdout or "") + (out.stderr or "")
        lines = [ln for ln in merged.splitlines() if ln.strip()]
        print(json.dumps({"ok": True, "alias": alias, "container": name, "lines": lines}))
        return 0
    except subprocess.SubprocessError as e:
        print(json.dumps({"ok": False, "error": str(e), "missing": False}))
        return 1


def _catalog_entry(alias: str):
    catalog, _ = cfg_mod.load_catalog()
    return next((m for m in catalog if m["id"] == alias), None)


def _stdin_text() -> str:
    """Read stdin. Accepts either raw text, or base64 if prefixed with `b64:`.

    The widget uses base64 to ship multi-line UTF-8 with quotes through
    Plasma's executable DataSource without quoting headaches.
    """
    raw = sys.stdin.read()
    if raw.startswith("b64:"):
        try:
            return base64.b64decode(raw[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return raw  # fall back to literal
    return raw


def cmd_get_prompt(alias: str):
    entry = _catalog_entry(alias)
    if not entry:
        print(json.dumps({"ok": False, "error": f"unknown alias '{alias}'"}))
        return 1
    info = overrides.resolve_prompt(alias, entry)
    print(json.dumps({"ok": True, "alias": alias, **info}))
    return 0


def cmd_set_prompt(alias: str):
    entry = _catalog_entry(alias)
    if not entry:
        print(json.dumps({"ok": False, "error": f"unknown alias '{alias}'"}))
        return 1
    if entry.get("system_prompt"):
        print(json.dumps({
            "ok": False,
            "error": "this alias has an inline system_prompt in the catalog; "
                     "the inline value will keep winning even if a file is saved. "
                     "Override the catalog entry with a user catalog override "
                     "(~/.config/hydra-llm/catalog.yaml) before using set-prompt.",
            "path": str(overrides.prompt_path(alias)),
        }))
        return 1
    content = _stdin_text()
    p = overrides.write_prompt(alias, content)
    print(json.dumps({"ok": True, "alias": alias, "path": str(p), "bytes": len(content)}))
    return 0


def cmd_clear_prompt(alias: str):
    entry = _catalog_entry(alias)
    if not entry:
        print(json.dumps({"ok": False, "error": f"unknown alias '{alias}'"}))
        return 1
    if entry.get("system_prompt"):
        print(json.dumps({
            "ok": False,
            "error": "this alias has an inline system_prompt in the catalog; "
                     "edit your user catalog override to remove it.",
        }))
        return 1
    removed = overrides.clear_prompt(alias)
    print(json.dumps({"ok": True, "alias": alias, "removed": removed,
                      "path": str(overrides.prompt_path(alias))}))
    return 0


def cmd_get_params(alias: str):
    entry = _catalog_entry(alias)
    if not entry:
        print(json.dumps({"ok": False, "error": f"unknown alias '{alias}'"}))
        return 1
    info = overrides.resolve_params(alias, entry)
    info["defaults"] = overrides.PARAM_DEFAULTS
    print(json.dumps({"ok": True, "alias": alias, **info}))
    return 0


def cmd_set_params(alias: str):
    entry = _catalog_entry(alias)
    if not entry:
        print(json.dumps({"ok": False, "error": f"unknown alias '{alias}'"}))
        return 1
    raw = _stdin_text()
    try:
        incoming = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad JSON: {e}"}))
        return 1
    if not isinstance(incoming, dict):
        print(json.dumps({"ok": False, "error": "expected a JSON object"}))
        return 1
    p, saved, rejected = overrides.write_params(alias, incoming)
    print(json.dumps({"ok": True, "alias": alias, "path": str(p),
                      "saved": saved, "rejected": rejected}))
    return 0


def cmd_clear_params(alias: str):
    entry = _catalog_entry(alias)
    if not entry:
        print(json.dumps({"ok": False, "error": f"unknown alias '{alias}'"}))
        return 1
    removed = overrides.clear_params(alias)
    print(json.dumps({"ok": True, "alias": alias, "removed": removed,
                      "path": str(overrides.params_path(alias))}))
    return 0


# Terminal emulator preference. First found on PATH wins. Each entry is the
# command and the argv pattern needed to run a fresh shell with our chat command.
TERMINAL_CHAIN = [
    ("konsole",         lambda cmd: ["konsole", "-e", "bash", "-lc", cmd]),
    ("gnome-terminal",  lambda cmd: ["gnome-terminal", "--", "bash", "-lc", cmd]),
    ("alacritty",       lambda cmd: ["alacritty", "-e", "bash", "-lc", cmd]),
    ("kitty",           lambda cmd: ["kitty", "bash", "-lc", cmd]),
    ("xfce4-terminal",  lambda cmd: ["xfce4-terminal", "-x", "bash", "-lc", cmd]),
    ("xterm",           lambda cmd: ["xterm", "-e", "bash", "-lc", cmd]),
    ("x-terminal-emulator", lambda cmd: ["x-terminal-emulator", "-e", "bash", "-lc", cmd]),
]


def cmd_chat_spawn(alias: str):
    """Spawn the user's preferred terminal emulator running `hydra-llm chat <alias>`."""
    chosen = None
    for name, builder in TERMINAL_CHAIN:
        if shutil.which(name):
            chosen = (name, builder)
            break
    if not chosen:
        print(json.dumps({"ok": False, "error": "no supported terminal emulator on PATH"}))
        return 1
    name, builder = chosen
    chat_cmd = f"hydra-llm chat {alias}; echo; read -p 'Press enter to close...' _"
    try:
        subprocess.Popen(
            builder(chat_cmd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        print(json.dumps({"ok": False, "error": f"could not spawn {name}: {e}"}))
        return 1
    print(json.dumps({"ok": True, "terminal": name, "alias": alias}))
    return 0


def main(argv):
    if not argv:
        print(json.dumps({"ok": False, "error": "tray subcommand required"}), file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "status":
        return cmd_status()
    if cmd == "logs":
        if len(argv) < 2:
            print(json.dumps({"ok": False, "error": "logs requires <alias>"}))
            return 2
        tail = 200
        # parse --tail N if present
        if "--tail" in argv[1:]:
            i = argv.index("--tail")
            try:
                tail = int(argv[i + 1])
            except (IndexError, ValueError):
                tail = 200
        return cmd_logs(argv[1], tail=tail)
    if cmd == "chat-spawn":
        if len(argv) < 2:
            print(json.dumps({"ok": False, "error": "chat-spawn requires <alias>"}))
            return 2
        return cmd_chat_spawn(argv[1])
    if cmd in ("get-prompt", "set-prompt", "clear-prompt",
               "get-params", "set-params", "clear-params"):
        if len(argv) < 2:
            print(json.dumps({"ok": False, "error": f"{cmd} requires <alias>"}))
            return 2
        alias = argv[1]
        dispatch = {
            "get-prompt":   cmd_get_prompt,
            "set-prompt":   cmd_set_prompt,
            "clear-prompt": cmd_clear_prompt,
            "get-params":   cmd_get_params,
            "set-params":   cmd_set_params,
            "clear-params": cmd_clear_params,
        }
        return dispatch[cmd](alias)
    print(json.dumps({"ok": False, "error": f"unknown tray subcommand: {cmd}"}))
    return 2
