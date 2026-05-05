"""hydra-llm command-line entry point."""
import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    __version__, chat as chat_mod, config as cfg_mod, desktop, docker_driver, downloader,
    hardware, paths, personas as personas_mod, setup as setup_mod, tray as tray_mod,
)


DISCLAIMER_LINES = [
    "hydra-llm: NO WARRANTY. You alone are responsible for hardware, data, and model output.",
    "Read the LICENSE and the README disclaimer before relying on this tool.",
]


def main():
    parser = argparse.ArgumentParser(
        prog="hydra-llm",
        description="Run local LLMs the easy way.",
        epilog="\n".join(DISCLAIMER_LINES),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"hydra-llm {__version__}")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output where applicable")

    # Subcommands that produce data also accept --json directly, so users can
    # write `hydra-llm status --json` (more common style) instead of needing
    # `hydra-llm --json status`. Parents= mounts the same flag on each.
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", dest="json",
                             help="machine-readable JSON output")

    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="detect hardware and recommend a tier",
                   parents=[json_parent]).set_defaults(func=cmd_doctor)

    p = sub.add_parser("setup", help="first-run: build image, fetch starter model, smoke test")
    p.add_argument("--no-build", action="store_true", help="skip docker image build")
    p.add_argument("--no-download", action="store_true", help="skip starter model download")
    p.add_argument("--no-test", action="store_true", help="skip the smoke-test stage")
    p.add_argument("--model", default=None, help="starter model id (default: tinyllama-1.1b)")
    p.add_argument("--image", choices=["auto", "vulkan", "cpu"], default="auto",
                   help="force a specific image variant instead of auto-detecting")
    p.set_defaults(func=cmd_setup)

    sub.add_parser("status", help="list running model containers",
                   parents=[json_parent]).set_defaults(func=cmd_status)
    sub.add_parser("list", help="list configured/downloaded/running models",
                   parents=[json_parent]).set_defaults(func=cmd_list)

    p = sub.add_parser("list-online", help="show models in the catalog (filtered by hardware)",
                       parents=[json_parent])
    p.add_argument("--all", action="store_true", help="include models that don't fit your hardware")
    p.add_argument("--tier", help="filter to a specific tier id")
    p.set_defaults(func=cmd_list_online)

    p = sub.add_parser("download", help="download a model from the catalog",
                       parents=[json_parent])
    p.add_argument("alias", help="catalog id (e.g. gemma-2-2b)")
    p.add_argument("--force", action="store_true", help="re-download even if file exists")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("remove", help="delete a downloaded model file")
    p.add_argument("alias")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("start", help="start a model server", parents=[json_parent])
    p.add_argument("alias")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop a model server")
    p.add_argument("alias")
    p.set_defaults(func=cmd_stop)

    sub.add_parser("stop-all", help="stop every model server we manage").set_defaults(func=cmd_stop_all)

    p = sub.add_parser("api", help="print API URLs and a sample request for a running model")
    p.add_argument("alias")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("chat", help="interactive chat with a model")
    p.add_argument("alias")
    p.add_argument("--persona", "-p", help="name of a persona file (without extension) or a path")
    p.add_argument("--session", "-s", default="default", help="session name (resumes if exists)")
    p.add_argument("--temperature", type=float)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--no-thoughts", action="store_true", help="hide reasoning output")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("persona", help="manage personas")
    p_sub = p.add_subparsers(dest="persona_cmd")
    p_sub.add_parser("list", help="list personas").set_defaults(func=cmd_persona_list)
    pp = p_sub.add_parser("show", help="show a persona's contents")
    pp.add_argument("name")
    pp.set_defaults(func=cmd_persona_show)
    pp = p_sub.add_parser("path", help="print the personas directory")
    pp.set_defaults(func=cmd_persona_path)
    p.set_defaults(func=lambda a: (p.print_help() or sys.exit(1)) if not a.persona_cmd else None)

    sub.add_parser("config-path", help="print the config directory").set_defaults(func=cmd_config_path)

    p = sub.add_parser("uninstall", help="remove the install (keeps user data)")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("wipe", help="remove everything: install, configs, models, sessions, Docker image")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompt (DANGEROUS)")
    p.set_defaults(func=cmd_wipe)

    # Tray subcommand: GUI integration helper. Subsubcommands handled in tray.py.
    p = sub.add_parser("tray", help="GUI helpers (used by hydra-llm-plasma)")
    p.add_argument("tray_args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_tray)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(0)
    paths.ensure_user_dirs()
    sys.exit(args.func(args) or 0)


# --- doctor ------------------------------------------------------------------

def cmd_doctor(args):
    snap = hardware.system_snapshot()
    tier = hardware.detect_tier(snap)
    de = desktop.detect()
    if args.json:
        print(json.dumps({"hardware": snap, "tier": tier["id"], "tier_name": tier["name"],
                          "desktop": de},
                         default=str, indent=2))
        return 0
    cpu = snap["cpu"]
    print(f"CPU:    {cpu['cores']} cores  {cpu['model']}")
    print(f"RAM:    {snap['ram']['total_mb']} MiB total  ({snap['ram']['available_mb']} MiB available)")
    if snap["gpus"]:
        for g in snap["gpus"]:
            tag = " [iGPU]" if g.get("iGPU") else ""
            line = (f"GPU:    {g['name']}{tag}  VRAM {g['vram_used_mb']}/{g['vram_total_mb']} MiB"
                    f"  busy {g['util_pct']}%")
            if "gtt_used_mb" in g:
                line += f"  GTT {g['gtt_used_mb']}/{g['gtt_total_mb']} MiB"
            print(line)
    else:
        print("GPU:    none detected (CPU inference only)")
    print()
    print(f"Tier:   {tier['id']}  ({tier['name']})")
    print(f"Desktop: {de['name']}")
    print(f"         {de['hint']}")
    if de.get("widget_package") and not desktop.is_widget_installed(de["widget_package"]):
        print(f"         Install the panel widget:  sudo apt install {de['widget_package']}")
    print()
    print("Recommended next step:  hydra-llm list-online")
    return 0


# --- status / list ------------------------------------------------------------

def cmd_status(args):
    cfg = cfg_mod.load_user_config()
    rows, err = docker_driver.list_running(cfg)
    if err and not rows:
        if args.json:
            print(json.dumps({"ok": False, "error": err}))
        else:
            print(f"error: {err}", file=sys.stderr)
        return 1
    docker_driver.annotate_health(rows)
    if args.json:
        print(json.dumps({"ok": True, "running": rows}, indent=2))
        return 0
    if not rows:
        print("No model servers running.")
        return 0
    print(f"{'ALIAS':<24} {'PORT':<6} {'READY':<6} STATUS")
    for r in rows:
        ready = "yes" if r.get("ready") else "no"
        print(f"{r['alias']:<24} {r['port'] or '?':<6} {ready:<6} {r['status']}")
    return 0


def cmd_list(args):
    cfg = cfg_mod.load_user_config()
    catalog, _ = cfg_mod.load_catalog()
    running, _ = docker_driver.list_running(cfg)
    by_alias_running = {r["alias"]: r for r in running}
    snap = hardware.system_snapshot()
    out_rows = []
    for m in catalog:
        downloaded = downloader.is_downloaded(m, cfg)
        r = by_alias_running.get(m["id"])
        fits, why = hardware.fits_locally(m, snap)
        out_rows.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "size_gb": m.get("size_gb"),
            "downloaded": downloaded,
            "running": r is not None,
            "running_port": r["port"] if r else None,
            "fit": fits,
            "fit_why": why,
        })
    if args.json:
        print(json.dumps({"ok": True, "models": out_rows}, indent=2))
        return 0
    if not out_rows:
        print("Catalog is empty. Set HYDRA_LLM_CATALOG or install hydra-llm package.")
        return 1
    print(f"{'ID':<22} {'SIZE':<7} {'DOWNL':<7} {'RUN':<5} {'FIT':<6} NAME")
    for r in out_rows:
        size = f"{r['size_gb']} GB" if r['size_gb'] else "-"
        print(f"{r['id']:<22} {size:<7} {'yes' if r['downloaded'] else 'no':<7} "
              f"{'yes' if r['running'] else 'no':<5} {r['fit']:<6} {r['name']}")
    return 0


def cmd_list_online(args):
    cfg = cfg_mod.load_user_config()
    catalog, sources = cfg_mod.load_catalog()
    snap = hardware.system_snapshot()
    tier = hardware.detect_tier(snap)
    tier_filter = args.tier or tier["id"]

    def keep(m):
        if args.tier:
            return tier_filter in (m.get("recommended_for") or [])
        if args.all:
            return True
        fits, _ = hardware.fits_locally(m, snap)
        if fits == "no":
            return False
        if m.get("recommended_for"):
            return tier["id"] in m["recommended_for"]
        return True

    filtered = [m for m in catalog if keep(m)]
    if args.json:
        print(json.dumps({
            "ok": True,
            "tier": tier["id"],
            "sources": sources,
            "models": filtered,
        }, indent=2))
        return 0

    if not filtered:
        print("No matching models. Try `--all` to see everything in the catalog.")
        return 0

    print(f"Catalog sources: {', '.join(sources) if sources else '(none)'}")
    print(f"Detected tier:   {tier['id']}  ({tier['name']})\n")
    print(f"{'ID':<22} {'SIZE':<7} {'FIT':<6} {'DOWNL':<7} NAME")
    for m in filtered:
        fits, _ = hardware.fits_locally(m, snap)
        size = f"{m.get('size_gb', '?')} GB"
        downl = "yes" if downloader.is_downloaded(m, cfg) else "no"
        print(f"{m['id']:<22} {size:<7} {fits:<6} {downl:<7} {m.get('name', m['id'])}")
    print()
    print("Use:  hydra-llm download <id>     to fetch a model")
    print("      hydra-llm start    <id>     to launch its server")
    return 0


# --- download / remove --------------------------------------------------------

def _resolve_catalog(alias):
    catalog, _ = cfg_mod.load_catalog()
    for m in catalog:
        if m["id"] == alias:
            return m
    return None


def cmd_download(args):
    cfg = cfg_mod.load_user_config()
    entry = _resolve_catalog(args.alias)
    if not entry:
        msg = f"unknown catalog id: {args.alias}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
            print("       run `hydra-llm list-online` to see available ids", file=sys.stderr)
        return 1
    print(f"Downloading {entry['id']}  ({entry.get('size_gb', '?')} GB)")
    print(f"  source: {entry['url']}")
    try:
        path = downloader.download(entry, cfg, force=args.force)
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"saved to {path}")
    if args.json:
        print(json.dumps({"ok": True, "path": str(path)}))
    return 0


def cmd_remove(args):
    cfg = cfg_mod.load_user_config()
    entry = _resolve_catalog(args.alias)
    if not entry:
        print(f"error: unknown catalog id: {args.alias}", file=sys.stderr)
        return 1
    if not args.yes:
        ans = input(f"delete {entry['filename']}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 0
    if downloader.remove_local(entry, cfg):
        print(f"removed {entry['filename']}")
        return 0
    print("file was not present.")
    return 0


# --- start / stop / api -------------------------------------------------------

def cmd_start(args):
    cfg = cfg_mod.load_user_config()
    entry = _resolve_catalog(args.alias)
    if not entry:
        print(f"error: unknown catalog id: {args.alias}", file=sys.stderr)
        return 1
    if not downloader.is_downloaded(entry, cfg):
        print(f"error: {entry['id']} is not downloaded yet. Run: hydra-llm download {entry['id']}",
              file=sys.stderr)
        return 1
    ok, info = docker_driver.start_model(entry, cfg, port=args.port)
    if not ok:
        print(f"error: {info.get('error')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, **info}))
    else:
        if info.get("already_running"):
            print(f"{info['container']} is already running.")
        else:
            print(f"started {info['container']}  port {info['port']}  image {info['image']}")
            print(f"check:   curl -s http://localhost:{info['port']}/health")
    return 0


def cmd_stop(args):
    ok, name = docker_driver.stop(args.alias)
    if not ok:
        print(f"error: {name}", file=sys.stderr)
        return 1
    print(f"stopped {name}")
    return 0


def cmd_stop_all(args):
    ok, err, names = docker_driver.stop_all()
    if not ok:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if not names:
        print("nothing was running.")
        return 0
    for n in names:
        print(f"stopped {n}")
    return 0


def cmd_api(args):
    cfg = cfg_mod.load_user_config()
    rows, _ = docker_driver.list_running(cfg)
    match = next((r for r in rows if r["alias"] == args.alias), None)
    if not match:
        print(f"error: {args.alias} is not running", file=sys.stderr)
        return 1
    port = match["port"]
    base = f"http://localhost:{port}"
    print(f"Base URL: {base}")
    print()
    print("Sample request:")
    print(f"""  curl -s {base}/v1/chat/completions \\
    -H 'Content-Type: application/json' \\
    -d '{{"messages":[{{"role":"user","content":"hello"}}]}}'""")
    return 0


# --- chat ---------------------------------------------------------------------

def cmd_chat(args):
    cfg = cfg_mod.load_user_config()
    entry = _resolve_catalog(args.alias)
    if not entry:
        print(f"error: unknown catalog id: {args.alias}", file=sys.stderr)
        return 1

    persona = None
    if args.persona:
        try:
            persona = personas_mod.load_persona(args.persona)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    rows, _ = docker_driver.list_running(cfg)
    match = next((r for r in rows if r["alias"] == args.alias), None)
    container_name = None
    if not match:
        print(f"{args.alias} is not running. Starting it now.")
        ok, info = docker_driver.start_model(entry, cfg)
        if not ok:
            print(f"error: {info.get('error')}", file=sys.stderr)
            return 1
        port = info["port"]
        container_name = info["container"]
    else:
        port = match["port"]
        container_name = match["container"]

    base_url = f"http://localhost:{port}"
    cli_overrides = {}
    if args.temperature is not None: cli_overrides["temperature"] = args.temperature
    if args.max_tokens is not None: cli_overrides["max_tokens"] = args.max_tokens
    show_thoughts = not args.no_thoughts

    chat_mod.interactive_chat(
        base_url=base_url,
        persona=persona,
        alias=args.alias,
        catalog_entry=entry,
        session_name=args.session,
        show_thoughts=show_thoughts,
        cli_overrides=cli_overrides,
        container_name=container_name,
    )
    return 0


# --- personas -----------------------------------------------------------------

def cmd_persona_list(args):
    items = personas_mod.list_personas()
    if not items:
        print(f"no personas found in {paths.PERSONAS_DIR}")
        print("create one: drop a .md file with optional YAML front matter there.")
        return 0
    for name, p in items.items():
        print(f"{name:<20} {p}")
    return 0


def cmd_persona_show(args):
    try:
        per = personas_mod.load_persona(args.name)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"name:        {per.name}")
    print(f"source:      {per.source}")
    print(f"model:       {per.model or '(none)'}")
    print(f"temperature: {per.temperature if per.temperature is not None else '(default)'}")
    print(f"max_tokens:  {per.max_tokens if per.max_tokens is not None else '(default)'}")
    print()
    print("system prompt:")
    print("---")
    print(per.system_prompt)
    return 0


def cmd_persona_path(args):
    print(paths.PERSONAS_DIR)
    return 0


def cmd_config_path(args):
    print(paths.CONFIG_DIR)
    return 0


def cmd_setup(args):
    image_override = None if args.image == "auto" else args.image
    return setup_mod.run_setup(
        build=not args.no_build,
        download=not args.no_download,
        test=not args.no_test,
        model_id=args.model or setup_mod.DEFAULT_SMOKE_MODEL,
        image_override=image_override,
    )


def _find_uninstall_script():
    """Locate scripts/user-uninstall.sh. Looks next to the install dirs."""
    candidates = [
        Path(os.environ.get("HYDRA_LLM_SHARE", "")) / "scripts" / "user-uninstall.sh",
        Path(__file__).resolve().parent.parent.parent / "scripts" / "user-uninstall.sh",
        Path("/usr/share/hydra-llm/scripts/user-uninstall.sh"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _run_uninstall(mode: str, skip_confirm: bool):
    script = _find_uninstall_script()
    if not script:
        print("error: could not locate user-uninstall.sh", file=sys.stderr)
        return 1
    user_bin = Path.home() / ".local" / "bin"
    user_lib = Path.home() / ".local" / "share" / "hydra-llm" / "lib"
    user_share = Path.home() / ".local" / "share" / "hydra-llm" / "share"

    if not skip_confirm:
        if mode == "wipe":
            print("This will permanently delete:")
            print(f"  install:        {user_bin}/hydra-llm, {user_lib}, {user_share}")
            print(f"  user config:    {paths.CONFIG_DIR}")
            print(f"  chat sessions:  {paths.SESSIONS_DIR}")
            print(f"  cache:          {paths.CACHE_DIR}")
            print(f"  data dir:       {paths.DATA_DIR}  (includes downloaded GGUFs)")
            print(f"  Docker image:   hydra-llm/llama-server:*")
        else:
            print("This will remove the install. User data is kept:")
            print(f"  removes:        {user_bin}/hydra-llm, {user_lib}, {user_share}")
            print(f"  preserved:      {paths.CONFIG_DIR}, {paths.SESSIONS_DIR}, {paths.DATA_DIR}/models")
            print("  Docker image:   kept (run `docker rmi hydra-llm/llama-server:*` to remove)")
        ans = input(f"Continue? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 0

    import subprocess
    return subprocess.call(
        ["bash", str(script), str(user_bin), str(user_lib), str(user_share), mode]
    )


def cmd_uninstall(args):
    return _run_uninstall("keep-data", args.yes)


def cmd_wipe(args):
    return _run_uninstall("wipe", args.yes)


def cmd_tray(args):
    return tray_mod.main(args.tray_args or [])
