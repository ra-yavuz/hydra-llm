"""hydra-llm command-line entry point."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import (
    __version__, autostart as autostart_mod, chat as chat_mod, config as cfg_mod,
    desktop, docker_driver, downloader, hardware, paths,
    personas as personas_mod, rag_catalog as rag_cat_mod,
    rag_store as rag_store_mod,
    setup as setup_mod, tray as tray_mod,
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

    p = sub.add_parser("addlocal",
                       help="register an existing GGUF on disk as a catalog entry",
                       parents=[json_parent])
    p.add_argument("file", help="path to the .gguf file you want to register")
    p.add_argument("--id", dest="alias_id",
                   help="catalog id (default: derived from the filename)")
    p.add_argument("--name", help="human-readable name (default: derived from the filename)")
    p.add_argument("--tier", action="append", dest="tiers",
                   choices=["tiny", "laptop", "halo", "workstation", "server"],
                   help="hardware tier this model fits (repeat for multiple)")
    p.add_argument("--port", type=int, help="default host port when started")
    p.add_argument("--gpu-layers", type=int, default=99,
                   help="layers to offload to GPU (default: 99 = all)")
    p.add_argument("--ram-gb", type=float, dest="ram_gb",
                   help="minimum system RAM, GB (informational)")
    p.add_argument("--vram-gb", type=float, dest="vram_gb",
                   help="VRAM needed for full offload, GB (informational)")
    p.add_argument("--context", type=int, help="context window the model supports")
    p.add_argument("--family", help="model family tag, e.g. llama, qwen, gemma")
    p.add_argument("--license", help="license string, informational only")
    p.add_argument("--link", action="store_true",
                   help="symlink the file into models_dir if it lives elsewhere")
    p.add_argument("--replace", action="store_true",
                   help="overwrite an existing user-catalog entry with the same id")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_addlocal)

    p = sub.add_parser(
        "create",
        help="bake a persona into an existing model as a new catalog alias",
        parents=[json_parent],
        description=(
            "Create a new catalog alias by binding an existing model to a "
            "persona file. The persona's body becomes the new alias's inline "
            "system_prompt, and any front-matter temperature/max_tokens become "
            "its inline params; the underlying GGUF is shared with the base "
            "model (no extra download).\n\n"
            "Example:\n"
            "  hydra-llm create gemma-2-2b ~/personas/coolguy.md\n"
            "  -> creates alias 'gemma-2-2b-coolguy'\n\n"
            "  hydra-llm create gemma-2-2b ~/personas/coolguy.md mymodel\n"
            "  -> creates alias 'mymodel'\n\n"
            "After creation, `hydra-llm chat <new-alias>` jumps straight in "
            "with the persona applied; no --persona flag needed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("base", help="catalog id of an existing model to base the new alias on")
    p.add_argument("persona_file", help="path to the persona file (.md or .txt)")
    p.add_argument("new_id", nargs="?",
                   help="catalog id for the new alias (default: <base>-<persona-stem>)")
    p.add_argument("--port", type=int, help="override the auto-picked port")
    p.add_argument("--replace", action="store_true",
                   help="overwrite an existing user-catalog entry with the same id")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("remove", help="delete a downloaded model file")
    p.add_argument("alias")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("start", help="start a model server", parents=[json_parent])
    p.add_argument("alias", nargs="?",
                   help="catalog id, GGUF filename, or index from `status`/`list`. "
                        "Optional if exactly one model is downloaded.")
    p.add_argument("--port", type=int)
    p.add_argument("--no-wait", action="store_true",
                   help="skip the post-start /health poll and return immediately")
    p.add_argument("--wait-timeout", type=float, default=60.0,
                   help="seconds to wait for /health to return ok (default 60)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop a model server")
    p.add_argument("alias")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser(
        "stop-all",
        help="stop every model server we manage",
        description=(
            "Stop every container with the configured prefix. Freed VRAM is "
            "released by the GPU driver as soon as the containers exit. "
            "System RAM the kernel was using to cache the GGUF mmaps is "
            "reclaimed lazily by the kernel itself; pass --drop-caches to "
            "force an immediate reclaim via "
            "`echo 3 | sudo tee /proc/sys/vm/drop_caches`. The sudo step "
            "is invoked interactively the first time it is needed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--drop-caches", action="store_true",
                   help="also drop the kernel page cache (needs sudo)")
    p.set_defaults(func=cmd_stop_all)

    p = sub.add_parser("autostart",
                       help="start a chosen model when you log in (user systemd unit)",
                       parents=[json_parent])
    p.add_argument("alias", nargs="?",
                   help="catalog id, GGUF filename, or filename without .gguf")
    p.add_argument("--off", "--disable", action="store_true",
                   help="disable autostart and remove the unit")
    p.set_defaults(func=cmd_autostart)

    p = sub.add_parser(
        "predict",
        help="set the default cap on tokens generated when the client does "
             "not send max_tokens",
        parents=[json_parent],
        description=(
            "Sets the server-side fallback for clients that don't send "
            "max_tokens. Clients that do send max_tokens always win; this "
            "only changes the default.\n\n"
            "Accepted values:\n"
            "  uncapped    no cap; stop on EOS or context full (recommended)\n"
            "  off         don't pass --predict; llama-server's built-in 128 applies\n"
            "  <integer>   any positive integer, e.g. 2048\n"
            "\n"
            "Run with no arguments to show the current value. Takes effect "
            "for newly started containers; restart running ones to apply."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("value", nargs="?",
                   help="uncapped | off | <integer>")
    p.set_defaults(func=cmd_predict)

    p = sub.add_parser(
        "reasoning",
        help="control how the model's 'thinking' / chain-of-thought is exposed",
        parents=[json_parent],
        description=(
            "Sets llama-server's --reasoning-format flag.\n\n"
            "Accepted values:\n"
            "  none      thinking stays inline in `content` (e.g. <think>...</think>).\n"
            "            Default; most plug-and-play.\n"
            "  deepseek  thinking is split out into a separate `reasoning_content`\n"
            "            field on each streamed delta. Clients can fold it.\n"
            "  hide      strip thinking before returning to the client (--reasoning-format auto).\n"
            "  off       don't pass the flag; use llama-server's compiled-in default.\n"
            "\n"
            "Run with no arguments to show the current value. Takes effect for "
            "newly started containers; restart running ones to apply."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("value", nargs="?",
                   help="none | deepseek | hide | off")
    p.set_defaults(func=cmd_reasoning)

    p = sub.add_parser("api", help="print API URLs and a sample request for a running model")
    p.add_argument("alias")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("chat", help="interactive chat with a model")
    p.add_argument("alias")
    p.add_argument("session_file", nargs="?",
                   help="path to a session JSON file (resumes if it exists, "
                        "creates it otherwise). Overrides --session when given. "
                        "Useful to keep a chat log next to the project it's about: "
                        "`hydra-llm chat gemma-2-2b ./notes.json`.")
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

    # --- rag (retrieval-augmented generation) -------------------------------
    p_rag = sub.add_parser(
        "rag",
        help="manage embedders and indexed corpora for RAG",
        description=(
            "Embedders are a separate model species from chat models; they "
            "produce fixed-size vectors and run via `llama-server "
            "--embeddings`. Use `hydra-llm rag setup` for first-run, "
            "`hydra-llm index <path>` to index a folder, and "
            "`hydra-llm chat <alias> --rag <path>` to chat with retrieval."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rag_sub = p_rag.add_subparsers(dest="rag_cmd")

    pp = rag_sub.add_parser(
        "list-online", help="list available embedders, filtered to your hardware",
        parents=[json_parent],
    )
    pp.add_argument("--all", action="store_true",
                    help="include embedders that don't fit your hardware")
    pp.add_argument("--tier", help="filter to a specific tier id")
    pp.set_defaults(func=cmd_rag_list_online)

    pp = rag_sub.add_parser(
        "list", help="list installed embedders",
        parents=[json_parent],
    )
    pp.set_defaults(func=cmd_rag_list)

    pp = rag_sub.add_parser(
        "download", help="download an embedder GGUF",
        parents=[json_parent],
    )
    pp.add_argument("alias", help="embedder id (e.g. nomic-embed-text)")
    pp.add_argument("--force", action="store_true",
                    help="re-download even if file exists")
    pp.set_defaults(func=cmd_rag_download)

    pp = rag_sub.add_parser(
        "remove", help="delete an installed embedder GGUF",
    )
    pp.add_argument("alias")
    pp.add_argument("--yes", action="store_true", help="skip confirmation")
    pp.set_defaults(func=cmd_rag_remove)

    pp = rag_sub.add_parser(
        "info", help="show details about an embedder",
        parents=[json_parent],
    )
    pp.add_argument("alias")
    pp.set_defaults(func=cmd_rag_info)

    pp = rag_sub.add_parser(
        "stores", help="list folders that have been indexed",
        parents=[json_parent],
    )
    pp.add_argument("--prune", action="store_true",
                    help="drop registry entries whose folder no longer exists")
    pp.set_defaults(func=cmd_rag_stores)

    p_rag.set_defaults(func=lambda a: (p_rag.print_help() or sys.exit(1)) if not a.rag_cmd else None)

    # --- top-level index / query commands -----------------------------------
    p = sub.add_parser(
        "index",
        help="build or refresh a RAG index for a folder",
        parents=[json_parent],
        description=(
            "Walk the given folder (default: cwd), classify files as code "
            "or prose, chunk them, embed each chunk with the appropriate "
            "embedder, and store the result in <folder>/.hydra-index/.\n\n"
            "Idempotent: re-running compares (path, mtime, size) against "
            "the previous index and only re-embeds changed files. Use "
            "--full to force a rebuild from scratch.\n\n"
            "Respects .gitignore plus a builtin blacklist (node_modules, "
            ".venv, build artefacts, binary files, files >1 MB)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("path", nargs="?", default=".", help="folder to index (default: cwd)")
    p.add_argument("--embedder", help="explicit embedder id (overrides auto code/prose split)")
    p.add_argument("--code-embedder", help="embedder to use for code files")
    p.add_argument("--prose-embedder", help="embedder to use for prose files")
    p.add_argument("--single-index", action="store_true",
                   help="embed everything with one embedder (no code/prose split)")
    p.add_argument("--no-code", action="store_true", help="skip code files")
    p.add_argument("--no-prose", action="store_true", help="skip prose files")
    p.add_argument("--exclude", action="append", default=[],
                   help="glob to skip (repeatable; layered on top of .gitignore)")
    p.add_argument("--include", action="append", default=[],
                   help="glob to force-include (repeatable; overrides excludes)")
    p.add_argument("--depth", type=int, help="max recursion depth")
    p.add_argument("--max-file-size-mb", type=float, default=1.0,
                   help="skip files larger than this (default: 1 MB)")
    p.add_argument("--full", action="store_true", help="force a full rebuild")
    p.add_argument("--dry-run", action="store_true", help="print plan, don't embed")
    p.add_argument("--no-register", action="store_true",
                   help="don't add this path to the global stores registry")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser(
        "query",
        help="search a RAG index without entering chat",
        parents=[json_parent],
        description=(
            "Embed the query, search the index, return top-K chunks ordered "
            "by Reciprocal Rank Fusion across the code and prose indexes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("text", help="natural-language query")
    p.add_argument("--in", dest="in_path", default=".",
                   help="folder containing the .hydra-index/ to search (default: cwd)")
    p.add_argument("--top-k", type=int, default=5, help="how many results to return")
    p.add_argument("--code-only", action="store_true", help="search only the code index")
    p.add_argument("--prose-only", action="store_true", help="search only the prose index")
    p.set_defaults(func=cmd_query)

    sub.add_parser("config-path", help="print the config directory").set_defaults(func=cmd_config_path)

    p = sub.add_parser(
        "config",
        help="show or change per-model server-launch settings",
        parents=[json_parent],
        description=(
            "Get or set per-alias server-launch overrides such as "
            "reasoning_format, predict, extra_args, and "
            "chat_template_kwargs.\n\n"
            "Layer order (narrowest wins):\n"
            "  llama-server defaults  <  catalog entry  <  config.yaml  <\n"
            "  per-alias override (this command).\n\n"
            "Forms:\n"
            "  hydra-llm config <alias>                     show resolved settings\n"
            "  hydra-llm config <alias> <key>               show one effective key\n"
            "  hydra-llm config <alias> <key> <value>       set the override\n"
            "  hydra-llm config <alias> reset               drop all overrides\n"
            "  hydra-llm config <alias> reset <key>         drop one override\n\n"
            "When the alias is currently running and a launch-time "
            "setting changes, you'll be prompted to restart it. Pass "
            "--restart to skip the prompt and just restart, or --no-restart "
            "to skip the restart even if needed (the new value applies on "
            "next start)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("alias")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--restart", action="store_true",
                   help="restart a running container without asking")
    p.add_argument("--no-restart", action="store_true",
                   help="don't restart even if the change is launch-time")
    p.set_defaults(func=cmd_config)

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

    p = sub.add_parser("help", help="show help for hydra-llm or a specific subcommand")
    p.add_argument("topic", nargs="?", help="subcommand name (e.g. 'start', 'download')")
    p.set_defaults(func=lambda a: _cmd_help(parser, sub, a))

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(0)
    paths.ensure_user_dirs()
    sys.exit(args.func(args) or 0)


def _cmd_help(parser, sub, args):
    topic = getattr(args, "topic", None)
    if not topic:
        parser.print_help()
        return 0
    choices = sub.choices
    if topic not in choices:
        print(f"hydra-llm help: unknown subcommand '{topic}'", file=sys.stderr)
        print(f"available: {', '.join(sorted(choices))}", file=sys.stderr)
        return 2
    choices[topic].print_help()
    return 0


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
    # Show every alias the user can refer to by `#`, so the index printed here
    # matches exactly what `start <#>` / `stop <#>` will resolve to. Entries
    # that are downloaded but have no container appear with "-" status.
    ordered = _enumerated_aliases(cfg)
    by_alias = {r["alias"]: r for r in rows}
    display = []
    for i, alias in enumerate(ordered, 1):
        r = by_alias.get(alias)
        if r is not None:
            display.append({
                "index": i,
                "alias": alias,
                "container": r["container"],
                "state": r["state"],
                "port": r["port"],
                "status": r["status"],
                "ready": r.get("ready", False),
            })
        else:
            display.append({
                "index": i,
                "alias": alias,
                "container": None,
                "state": "not started",
                "port": None,
                "status": "not started",
                "ready": False,
            })
    if args.json:
        print(json.dumps({"ok": True, "rows": display}, indent=2))
        return 0
    if not display:
        print("No models downloaded and no containers found.")
        print("Browse: hydra-llm list-online")
        return 0
    print(f"{'#':<3} {'ALIAS':<32} {'PORT':<6} {'READY':<6} STATUS")
    for d in display:
        ready = "yes" if d["ready"] else "no"
        print(f"{d['index']:<3} {d['alias']:<32} {d['port'] or '-':<6} {ready:<6} {d['status']}")
    print()
    print("Tip: `hydra-llm start <#>` / `stop <#>` / `autostart <#>` accept these index numbers.")
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
    ordered = _enumerated_aliases(cfg)
    index_by_alias = {a: i + 1 for i, a in enumerate(ordered)}
    for r in out_rows:
        r["index"] = index_by_alias.get(r["id"])  # only downloaded entries get a #
    if args.json:
        print(json.dumps({"ok": True, "models": out_rows}, indent=2))
        return 0
    if not out_rows:
        print("Catalog is empty. Set HYDRA_LLM_CATALOG or install hydra-llm package.")
        return 1
    print(f"{'#':<3} {'ID':<22} {'SIZE':<7} {'DOWNL':<7} {'RUN':<5} {'FIT':<6} NAME")
    for r in out_rows:
        size = f"{r['size_gb']} GB" if r['size_gb'] else "-"
        idx = r["index"] if r["index"] is not None else "-"
        print(f"{idx:<3} {r['id']:<22} {size:<7} {'yes' if r['downloaded'] else 'no':<7} "
              f"{'yes' if r['running'] else 'no':<5} {r['fit']:<6} {r['name']}")
    print()
    print("Tip: `hydra-llm start <#>` and `stop <#>` accept the index numbers above.")
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
    """Find a catalog entry by id, by GGUF filename, or by filename without
    extension. Exact match in that order. Returns None if nothing matches."""
    catalog, _ = cfg_mod.load_catalog()
    for m in catalog:
        if m["id"] == alias:
            return m
    for m in catalog:
        fn = m.get("filename") or ""
        if fn == alias:
            return m
        if fn.endswith(".gguf") and fn[:-len(".gguf")] == alias:
            return m
    return None


def _enumerated_aliases(cfg=None):
    """Stable, alphabetically-sorted list of aliases the user can refer to by
    index. Includes any container we manage (running or exited) and every
    downloaded catalog entry, deduplicated. The order is fixed by sorted alias
    so `1` keeps meaning the same thing across consecutive commands."""
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    aliases = set()
    rows, _ = docker_driver.list_running(cfg)
    for r in rows:
        aliases.add(r["alias"])
    catalog, _ = cfg_mod.load_catalog()
    for m in catalog:
        if downloader.is_downloaded(m, cfg):
            aliases.add(m["id"])
    return sorted(aliases)


def _resolve_alias_or_index(arg, cfg=None):
    """Turn a user-supplied positional into a concrete alias.

    Resolution order:
      1. catalog id / filename / filename-without-.gguf (delegates to _resolve_catalog).
      2. small positive integer -> 1-based index into _enumerated_aliases().

    Returns (alias, error_message). On success, error_message is None.
    """
    if not arg:
        return None, "no alias given"
    entry = _resolve_catalog(arg)
    if entry:
        return entry["id"], None
    if arg.isdigit():
        idx = int(arg)
        ordered = _enumerated_aliases(cfg)
        if 1 <= idx <= len(ordered):
            return ordered[idx - 1], None
        return None, (f"index {idx} out of range; "
                      f"have {len(ordered)} entries (run `hydra-llm status` to see them)")
    return None, f"unknown alias: {arg}"


def _slug_from_filename(stem: str) -> str:
    """SmolLM2-360M-Instruct-Q4_K_M -> smollm2-360m-instruct."""
    import re
    s = stem
    # Drop common quant suffixes so the id stays readable.
    s = re.sub(r"[-_.](Q\d+(_[A-Z0-9]+)*|F16|F32|BF16|IQ\d+(_[A-Z0-9]+)*)$",
               "", s, flags=re.IGNORECASE)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "model"


def _next_free_port(cfg, taken: set[int]) -> int | None:
    lo, hi = cfg.get("port_range", [18080, 18099])
    for p in range(lo, hi + 1):
        if p not in taken:
            return p
    return None


def cmd_addlocal(args):
    cfg = cfg_mod.load_user_config()
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        msg = f"file not found: {src}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    if src.suffix.lower() != ".gguf":
        print(f"warning: {src.name} does not end in .gguf; proceeding anyway",
              file=sys.stderr)

    models_dir = Path(cfg.get("models_dir") or paths.MODELS_DIR_DEFAULT).expanduser().resolve()
    models_dir.mkdir(parents=True, exist_ok=True)

    # The container bind-mounts models_dir at /models, so the file MUST live
    # under that root. Either the user picked a path inside it, or we symlink.
    try:
        src.relative_to(models_dir)
        in_models_dir = True
    except ValueError:
        in_models_dir = False

    if not in_models_dir:
        if not args.link:
            msg = (f"file is at {src}\n"
                   f"    but models_dir is {models_dir}\n"
                   f"    options:\n"
                   f"      - move/copy the file into {models_dir}\n"
                   f"      - point models_dir at the file's folder in ~/.config/hydra-llm/config.yaml\n"
                   f"      - re-run with --link to create a symlink at {models_dir}/{src.name}")
            if args.json:
                print(json.dumps({"ok": False, "error": "file outside models_dir"}))
            else:
                print(f"error: {msg}", file=sys.stderr)
            return 1
        link_path = models_dir / src.name
        if link_path.exists() or link_path.is_symlink():
            if link_path.resolve() != src:
                print(f"error: {link_path} already exists and points elsewhere", file=sys.stderr)
                return 1
        else:
            link_path.symlink_to(src)
        on_disk_filename = src.name
    else:
        on_disk_filename = src.name

    alias = args.alias_id or _slug_from_filename(src.stem)
    name = args.name or src.stem.replace("_", " ").replace("-", " ")

    size_gb = round(src.stat().st_size / (1024 ** 3), 2)

    running, _ = docker_driver.list_running(cfg)
    taken_ports = {r["port"] for r in running if r["port"]}
    if args.port:
        port = args.port
    else:
        # Avoid colliding with the shipped catalog's reserved ports.
        catalog, _ = cfg_mod.load_catalog()
        for m in catalog:
            if m.get("default_port"):
                taken_ports.add(m["default_port"])
        port = _next_free_port(cfg, taken_ports)
        if port is None:
            print("error: no free port in port_range; pass --port", file=sys.stderr)
            return 1

    entry: dict = {
        "id": alias,
        "name": name,
        "filename": on_disk_filename,
        "size_gb": size_gb,
        "default_port": port,
        "gpu_layers": args.gpu_layers,
        "recommended_for": args.tiers or [],
        "tags": ["local"],
    }
    if args.ram_gb is not None:
        entry["needs_ram_gb"] = args.ram_gb
    if args.vram_gb is not None:
        entry["fits_in_vram_gb"] = args.vram_gb
    if args.context is not None:
        entry["context"] = args.context
    if args.family:
        entry["family"] = args.family
    if args.license:
        entry["license"] = args.license

    if not args.json and not args.yes:
        print("Will register this entry in ~/.config/hydra-llm/catalog.yaml:\n")
        print(f"  id:           {entry['id']}")
        print(f"  name:         {entry['name']}")
        print(f"  filename:     {entry['filename']}")
        print(f"  models_dir:   {models_dir}" + ("  (symlink created)" if not in_models_dir else ""))
        print(f"  size:         {entry['size_gb']} GB")
        print(f"  default_port: {entry['default_port']}")
        print(f"  gpu_layers:   {entry['gpu_layers']}")
        if entry["recommended_for"]:
            print(f"  tiers:        {', '.join(entry['recommended_for'])}")
        if "needs_ram_gb" in entry:
            print(f"  needs_ram_gb: {entry['needs_ram_gb']}")
        if "fits_in_vram_gb" in entry:
            print(f"  fits_in_vram_gb: {entry['fits_in_vram_gb']}")
        ans = input("\nWrite entry? [Y/n] ").strip().lower()
        if ans and ans not in ("y", "yes"):
            print("aborted.")
            return 0

    try:
        path, replaced = cfg_mod.add_user_catalog_entry(entry, replace=args.replace)
    except cfg_mod.CatalogError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1

    verb = "replaced" if replaced else "added"
    if args.json:
        print(json.dumps({"ok": True, "id": entry["id"], "path": str(path),
                          "replaced": replaced}, indent=2))
    else:
        print(f"\n{verb} {entry['id']} in {path}")
        print(f"try:  hydra-llm start {entry['id']}")
        print(f"      hydra-llm chat  {entry['id']}")
    return 0


# Catalog fields we copy through from a base entry into a `create`-derived
# alias. We deliberately exclude `id`, `name`, `system_prompt`, `params`, and
# `default_port` because those are set fresh by `create`; everything else
# (filename, size, gpu_layers, family, license, recommended_for, tags, ...)
# describes the underlying weights and applies equally to the derived alias.
_CREATE_INHERITED_KEYS = (
    "filename", "size_gb", "gpu_layers", "recommended_for", "tags",
    "needs_ram_gb", "fits_in_vram_gb", "context", "family", "license",
    "url", "sha256", "default_reasoning_format", "extra_args",
)


def cmd_create(args):
    cfg = cfg_mod.load_user_config()

    base_entry = _resolve_catalog(args.base)
    if not base_entry:
        msg = f"unknown base model: {args.base}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1

    persona_path = Path(args.persona_file).expanduser().resolve()
    try:
        persona = personas_mod.load_persona(str(persona_path))
    except FileNotFoundError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1

    new_id = args.new_id or _slug_from_filename(f"{base_entry['id']}-{persona_path.stem}")

    # Port choice: explicit --port wins. Otherwise inherit the base model's
    # default_port. Persona-baked aliases are skins over the same GGUF, so
    # sharing the base's port reservation is the natural default; you can only
    # run one container on a port at a time anyway, and the user can stop the
    # base before running the alias (or pass --port for parallel use).
    if args.port:
        port = args.port
    elif base_entry.get("default_port"):
        port = base_entry["default_port"]
    else:
        running, _ = docker_driver.list_running(cfg)
        taken_ports = {r["port"] for r in running if r["port"]}
        catalog, _ = cfg_mod.load_catalog()
        for m in catalog:
            if m.get("default_port"):
                taken_ports.add(m["default_port"])
        port = _next_free_port(cfg, taken_ports)
        if port is None:
            print("error: no free port in port_range and base has no default_port; pass --port",
                  file=sys.stderr)
            return 1

    entry: dict = {
        "id": new_id,
        "name": f"{base_entry.get('name', base_entry['id'])} ({persona.name})",
    }
    for key in _CREATE_INHERITED_KEYS:
        if key in base_entry:
            entry[key] = base_entry[key]
    entry["default_port"] = port
    entry["system_prompt"] = persona.system_prompt

    inline_params: dict = {}
    if persona.temperature is not None:
        inline_params["temperature"] = float(persona.temperature)
    if persona.max_tokens is not None:
        inline_params["max_tokens"] = int(persona.max_tokens)
    if inline_params:
        entry["params"] = inline_params

    # Always tag the entry as a create-derived alias so future tooling can
    # distinguish baked aliases from raw `addlocal` entries without guessing.
    tags = list(entry.get("tags") or [])
    if "persona-baked" not in tags:
        tags.append("persona-baked")
    entry["tags"] = tags
    entry["base_id"] = base_entry["id"]
    entry["persona_source"] = str(persona_path)

    if not args.json and not args.yes:
        print(f"Will register this entry in {paths.USER_CATALOG}:\n")
        print(f"  id:           {entry['id']}")
        print(f"  name:         {entry['name']}")
        print(f"  base:         {base_entry['id']} (filename: {entry.get('filename')})")
        print(f"  persona:      {persona.name}  (from {persona_path})")
        print(f"  default_port: {entry['default_port']}")
        if inline_params:
            print(f"  params:       {inline_params}")
        first_line = entry["system_prompt"].splitlines()[0] if entry["system_prompt"] else ""
        if first_line:
            preview = first_line if len(first_line) <= 78 else first_line[:75] + "..."
            print(f"  prompt[0]:    {preview}")
        ans = input("\nWrite entry? [Y/n] ").strip().lower()
        if ans and ans not in ("y", "yes"):
            print("aborted.")
            return 0

    try:
        path, replaced = cfg_mod.add_user_catalog_entry(entry, replace=args.replace)
    except cfg_mod.CatalogError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1

    verb = "replaced" if replaced else "created"
    if args.json:
        print(json.dumps({"ok": True, "id": entry["id"], "base": base_entry["id"],
                          "persona": persona.name, "path": str(path),
                          "replaced": replaced}, indent=2))
    else:
        print(f"\n{verb} {entry['id']} in {path}")
        print(f"try:  hydra-llm chat {entry['id']}")
    return 0


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
    if args.alias:
        alias, err = _resolve_alias_or_index(args.alias, cfg)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 1
        entry = _resolve_catalog(alias)
        if not entry:
            print(f"error: unknown catalog id: {alias}", file=sys.stderr)
            return 1
        if not downloader.is_downloaded(entry, cfg):
            print(f"error: {entry['id']} is not downloaded yet. Run: hydra-llm download {entry['id']}",
                  file=sys.stderr)
            return 1
        # If user passed a numeric index (or filename) make the resolution visible
        # before docker runs, so they can ctrl-c if it isn't what they meant.
        if not args.json and args.alias != entry["id"]:
            print(f"resolved {args.alias!r} -> {entry['id']}")
    else:
        catalog, _ = cfg_mod.load_catalog()
        downloaded = [m for m in catalog if downloader.is_downloaded(m, cfg)]
        if not downloaded:
            print("error: no models downloaded. Browse with: hydra-llm list-online",
                  file=sys.stderr)
            print("       then run: hydra-llm download <id>", file=sys.stderr)
            return 1
        if len(downloaded) > 1:
            print("error: multiple models available; specify which to start:", file=sys.stderr)
            for m in downloaded:
                print(f"  hydra-llm start {m['id']}", file=sys.stderr)
            return 1
        entry = downloaded[0]
    ok, info = docker_driver.start_model(entry, cfg, port=args.port)
    if not ok:
        print(f"error: {info.get('error')}", file=sys.stderr)
        return 1

    already = info.get("already_running", False)
    if not args.json:
        if already:
            print(f"{info['container']} is already running.")
        else:
            print(f"started {info['container']}  port {info['port']}  image {info['image']}")

    # Poll /health unless told not to. Skip the wait when we didn't actually
    # start anything new; the existing container's readiness is whatever it is.
    health = None
    if not args.no_wait and not already:
        if not args.json:
            sys.stdout.write("waiting for /health ")
            sys.stdout.flush()
        last_dot = [0.0]
        def tick(elapsed):
            if args.json:
                return
            # one dot every ~0.5s; the poll loop sleeps 0.5s so this is roughly 1:1.
            if elapsed - last_dot[0] >= 0.5:
                sys.stdout.write("."); sys.stdout.flush()
                last_dot[0] = elapsed
        health = docker_driver.wait_for_ready(
            info["container"], info["port"],
            timeout=args.wait_timeout, on_tick=tick,
        )
        if not args.json:
            print()  # close the dots line

    if args.json:
        out = {"ok": True, **info}
        if health is not None:
            out["health"] = health
        print(json.dumps(out))
        return 0 if (health is None or health["state"] == "ready" or already) else 1

    if health is None:
        # --no-wait or already_running path
        print(f"check:   curl -s http://localhost:{info['port']}/health")
        return 0
    if health["state"] == "ready":
        print(f"ready in {health['elapsed']:.1f}s on port {info['port']}")
        return 0
    if health["state"] == "loading":
        print(f"still loading after {health['elapsed']:.0f}s; the model may need more "
              f"time. Check with: curl -s http://localhost:{info['port']}/health")
        if health["logs"]:
            print("recent logs:")
            for line in health["logs"].splitlines():
                print(f"  {line}")
        return 0
    # exited
    print(f"container exited after {health['elapsed']:.1f}s.", file=sys.stderr)
    if health["logs"]:
        print("last logs:", file=sys.stderr)
        for line in health["logs"].splitlines():
            print(f"  {line}", file=sys.stderr)
    return 1


def cmd_autostart(args):
    if args.off:
        ok, msg = autostart_mod.disable()
        if args.json:
            print(json.dumps({"ok": ok, "message": msg}))
        else:
            print(msg)
        return 0 if ok else 1
    if not args.alias:
        st = autostart_mod.status()
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        if not st["model"]:
            print("autostart: off")
            print("set with: hydra-llm autostart <id>")
        else:
            state = "enabled" if st["enabled"] else "disabled"
            active = "" if st["active"] is None else f", active={st['active']}"
            print(f"autostart: {state}{active}")
            print(f"  model: {st['model']}")
            print(f"  unit:  {st['unit_path']}")
        return 0
    cfg = cfg_mod.load_user_config()
    alias, err = _resolve_alias_or_index(args.alias, cfg)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    entry = _resolve_catalog(alias)
    if not entry:
        print(f"error: unknown catalog id: {alias}", file=sys.stderr)
        return 1
    if not args.json and args.alias != entry["id"]:
        print(f"resolved {args.alias!r} -> {entry['id']}")
    ok, msg = autostart_mod.enable(entry["id"])
    if args.json:
        print(json.dumps({"ok": ok, "message": msg, "model": entry["id"]}))
    elif ok:
        print(msg)
    else:
        print(f"error: {msg}", file=sys.stderr)
    return 0 if ok else 1


def cmd_predict(args):
    cfg = cfg_mod.load_user_config()
    current = cfg.get("predict")
    if args.value is None:
        info = {
            "current": current,
            "accepted": ["uncapped", "off", "<positive integer>"],
            "note": "Clients that send max_tokens override this. Restart "
                    "running containers to apply changes.",
        }
        if args.json:
            print(json.dumps(info, indent=2))
            return 0
        print(f"predict: {current}")
        print("accepted values:")
        print("  uncapped    no cap; stop on EOS or context full (recommended)")
        print("  off         don't pass --predict; llama-server's built-in 128 applies")
        print("  <integer>   any positive integer, e.g. 2048")
        print("note: clients that send max_tokens override this. Restart "
              "running containers to apply.")
        return 0
    raw = args.value.strip().lower()
    if raw == "uncapped":
        new = "uncapped"
    elif raw == "off":
        new = "off"
    else:
        try:
            n = int(raw)
        except ValueError:
            print(f"error: not a valid value: {args.value!r}. "
                  "Try: uncapped, off, or a positive integer.", file=sys.stderr)
            return 1
        if n <= 0:
            print("error: integer must be positive (use 'uncapped' for no cap, "
                  "'off' to clear).", file=sys.stderr)
            return 1
        new = n
    cfg["predict"] = new
    path = cfg_mod.save_user_config(cfg)
    if args.json:
        print(json.dumps({"ok": True, "predict": new, "path": str(path)}))
    else:
        print(f"predict set to: {new}")
        print(f"  config: {path}")
        print("Restart any running containers to apply: hydra-llm stop <id> && hydra-llm start <id>")
    return 0


def cmd_reasoning(args):
    cfg = cfg_mod.load_user_config()
    current = cfg.get("reasoning_format")
    accepted = ["none", "deepseek", "hide", "off"]
    if args.value is None:
        info = {
            "current": current,
            "accepted": accepted,
            "note": "Restart running containers to apply changes.",
        }
        if args.json:
            print(json.dumps(info, indent=2))
            return 0
        print(f"reasoning: {current}")
        print("accepted values:")
        print("  none      thinking stays inline in `content` (default)")
        print("  deepseek  thinking goes to a separate `reasoning_content` field")
        print("  hide      strip thinking before returning to the client")
        print("  off       don't pass the flag; use llama-server's default")
        print("note: restart running containers to apply.")
        return 0
    raw = args.value.strip().lower()
    if raw not in accepted:
        print(f"error: not a valid value: {args.value!r}. "
              f"Try one of: {', '.join(accepted)}.", file=sys.stderr)
        return 1
    cfg["reasoning_format"] = raw
    path = cfg_mod.save_user_config(cfg)
    if args.json:
        print(json.dumps({"ok": True, "reasoning_format": raw, "path": str(path)}))
    else:
        print(f"reasoning set to: {raw}")
        print(f"  config: {path}")
        print("Restart any running containers to apply: hydra-llm stop <id> && hydra-llm start <id>")
    return 0


def cmd_stop(args):
    cfg = cfg_mod.load_user_config()
    alias, err = _resolve_alias_or_index(args.alias, cfg)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if args.alias != alias:
        print(f"resolved {args.alias!r} -> {alias}")
    print(f"stopping {alias}...")
    ok, name = docker_driver.stop(alias, cfg)
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
    else:
        for n in names:
            print(f"stopped {n}")

    drop = bool(getattr(args, "drop_caches", False))
    if not drop:
        return 0

    # Force the kernel to drop the page cache. We previously mmap'd
    # multi-GB GGUF files; even after the containers exit, the page
    # cache continues to hold them so a re-launch is fast. Drop them
    # explicitly when the user wants the RAM back. Needs root.
    print("dropping page cache (sudo)...")
    try:
        rc = subprocess.run(
            ["sudo", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
            timeout=30,
        ).returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  warn: could not run sudo drop_caches: {e}",
              file=sys.stderr)
        return 0
    if rc == 0:
        print("  ✓ page cache dropped")
    else:
        print(f"  warn: drop_caches exited {rc}; continuing.",
              file=sys.stderr)
    return 0


def cmd_api(args):
    cfg = cfg_mod.load_user_config()
    alias, err = _resolve_alias_or_index(args.alias, cfg)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    rows, _ = docker_driver.list_running(cfg)
    match = next((r for r in rows if r["alias"] == alias), None)
    if not match:
        print(f"error: {alias} is not running", file=sys.stderr)
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
    alias, err = _resolve_alias_or_index(args.alias, cfg)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    entry = _resolve_catalog(alias)
    if not entry:
        print(f"error: unknown catalog id: {alias}", file=sys.stderr)
        return 1

    persona = None
    if args.persona:
        try:
            persona = personas_mod.load_persona(args.persona)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    rows, _ = docker_driver.list_running(cfg)
    match = next((r for r in rows if r["alias"] == alias), None)
    container_name = None
    if not match:
        print(f"{alias} is not running. Starting it now.")
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

    session_file = None
    if args.session_file:
        session_file = Path(args.session_file).expanduser().resolve()
        if args.session != "default":
            print("error: pass either a session-file positional or --session, not both",
                  file=sys.stderr)
            return 1

    chat_mod.interactive_chat(
        base_url=base_url,
        persona=persona,
        alias=alias,
        catalog_entry=entry,
        session_name=args.session,
        session_file=session_file,
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


# --- rag / embedders ----------------------------------------------------------

def cmd_rag_list_online(args):
    cfg = cfg_mod.load_user_config()
    catalog, sources = rag_cat_mod.load_embedder_catalog()
    snap = hardware.system_snapshot()
    tier = hardware.detect_tier(snap)
    tier_filter = args.tier or tier["id"]

    def keep(e):
        if args.tier:
            return tier_filter in (e.get("recommended_for") or [])
        if args.all:
            return True
        fits, _ = hardware.fits_locally(e, snap)
        if fits == "no":
            return False
        if e.get("recommended_for"):
            return tier["id"] in e["recommended_for"]
        return True

    filtered = [e for e in catalog if keep(e)]
    if args.json:
        print(json.dumps({
            "ok": True,
            "tier": tier["id"],
            "sources": sources,
            "embedders": filtered,
        }, indent=2))
        return 0

    if not filtered:
        print("No matching embedders. Try `--all` to see everything.")
        return 0

    print(f"Embedder sources: {', '.join(sources) if sources else '(none)'}")
    print(f"Detected tier:    {tier['id']}  ({tier['name']})\n")
    print(f"{'ID':<22} {'KIND':<6} {'DIMS':<6} {'SIZE':<7} {'FIT':<6} {'DOWNL':<7} NAME")
    for e in filtered:
        fits, _ = hardware.fits_locally(e, snap)
        size = f"{e.get('size_gb', '?')} GB"
        downl = "yes" if rag_cat_mod.is_downloaded(e, cfg) else "no"
        print(f"{e['id']:<22} {e.get('kind', '?'):<6} "
              f"{str(e.get('dimensions', '?')):<6} {size:<7} {fits:<6} {downl:<7} "
              f"{e.get('name', e['id'])}")
    print()
    print("Use:  hydra-llm rag download <id>   to fetch an embedder")
    return 0


def cmd_rag_list(args):
    cfg = cfg_mod.load_user_config()
    catalog, _ = rag_cat_mod.load_embedder_catalog()
    installed = [e for e in catalog if rag_cat_mod.is_downloaded(e, cfg)]
    running, _ = docker_driver.list_running_embedders(cfg)
    by_alias = {r["alias"]: r for r in running if r.get("state") == "running"}
    if args.json:
        print(json.dumps({
            "ok": True,
            "embedders": [
                {**e,
                 "path": str(rag_cat_mod.embedder_path(e, cfg)),
                 "running": e["id"] in by_alias,
                 "port": by_alias.get(e["id"], {}).get("port")}
                for e in installed
            ],
        }, indent=2))
        return 0
    if not installed:
        print("No embedders installed.")
        print("Run `hydra-llm rag list-online` to see what's available,")
        print("then `hydra-llm rag download <id>` to install one.")
        return 0
    print(f"{'ID':<22} {'KIND':<6} {'DIMS':<6} {'SIZE':<8} {'STATUS':<22} NAME")
    for e in installed:
        size = f"{e.get('size_gb', '?')} GB"
        running_info = by_alias.get(e["id"])
        if running_info:
            status = f"running on :{running_info.get('port', '?')}"
        else:
            status = "idle"
        print(f"{e['id']:<22} {e.get('kind', '?'):<6} "
              f"{str(e.get('dimensions', '?')):<6} {size:<8} {status:<22} "
              f"{e.get('name', e['id'])}")
    return 0


def cmd_rag_download(args):
    cfg = cfg_mod.load_user_config()
    entry = rag_cat_mod.find_embedder(args.alias)
    if not entry:
        msg = f"unknown embedder id: {args.alias}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
            print("       run `hydra-llm rag list-online` to see available ids", file=sys.stderr)
        return 1
    print(f"Downloading {entry['id']}  ({entry.get('size_gb', '?')} GB)")
    print(f"  source: {entry['url']}")
    embedders_dir = Path(cfg.get("embedders_dir") or paths.EMBEDDERS_DIR_DEFAULT).expanduser()
    try:
        path = downloader.download(entry, cfg, force=args.force, dest_dir=embedders_dir)
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


def cmd_rag_remove(args):
    cfg = cfg_mod.load_user_config()
    entry = rag_cat_mod.find_embedder(args.alias)
    if not entry:
        print(f"error: unknown embedder id: {args.alias}", file=sys.stderr)
        return 1
    p = rag_cat_mod.embedder_path(entry, cfg)
    if not p.exists():
        print(f"{entry['id']} is not installed.")
        return 0
    if not args.yes:
        ans = input(f"delete {p}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted.")
            return 0
    p.unlink()
    print(f"removed {p}")
    return 0


def cmd_rag_info(args):
    cfg = cfg_mod.load_user_config()
    entry = rag_cat_mod.find_embedder(args.alias)
    if not entry:
        msg = f"unknown embedder id: {args.alias}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    p = rag_cat_mod.embedder_path(entry, cfg)
    running, _ = docker_driver.list_running_embedders(cfg)
    running_info = next((r for r in running
                         if r["alias"] == entry["id"] and r.get("state") == "running"),
                        None)
    if args.json:
        out = {**entry,
               "path": str(p),
               "installed": p.is_file(),
               "running": running_info is not None,
               "port": running_info.get("port") if running_info else None}
        print(json.dumps(out, indent=2))
        return 0
    print(f"id:              {entry['id']}")
    print(f"name:            {entry.get('name', entry['id'])}")
    print(f"family:          {entry.get('family', '?')}")
    print(f"kind:            {entry.get('kind', '?')}")
    print(f"dimensions:      {entry.get('dimensions', '?')}")
    print(f"max_tokens:      {entry.get('max_tokens', '?')}")
    print(f"pooling:         {entry.get('pooling', '?')}")
    if entry.get("query_prefix"):
        print(f"query prefix:    {entry['query_prefix']!r}")
    if entry.get("document_prefix"):
        print(f"document prefix: {entry['document_prefix']!r}")
    print(f"size:            {entry.get('size_gb', '?')} GB")
    print(f"license:         {entry.get('license', '?')}")
    print(f"file:            {p}")
    print(f"installed:       {'yes' if p.is_file() else 'no'}")
    if running_info:
        print(f"running:         yes, on :{running_info.get('port', '?')}")
    else:
        print(f"running:         no")
    if entry.get("notes"):
        print(f"\n{entry['notes']}")
    return 0


def cmd_rag_stores(args):
    if args.prune:
        dropped = rag_store_mod.prune_registry()
        if args.json:
            print(json.dumps({"ok": True, "dropped": dropped}, indent=2))
        elif dropped:
            for d in dropped:
                print(f"removed: {d}")
        else:
            print("nothing to prune.")
        return 0
    reg = rag_store_mod.load_registry()
    stores = reg.get("stores") or []
    enriched = []
    for s in stores:
        rp = Path(s.get("path", ""))
        if not (rp / rag_store_mod.INDEX_DIR_NAME).is_dir():
            enriched.append({**s, "missing": True})
            continue
        meta = rag_store_mod.read_meta(rp)
        info = {
            **s,
            "missing": False,
            "code_chunks": rag_store_mod.chunk_count(rp, "code"),
            "prose_chunks": rag_store_mod.chunk_count(rp, "prose"),
            "code_embedder": (meta.get("code_embedder") or {}).get("id"),
            "prose_embedder": (meta.get("prose_embedder") or {}).get("id"),
        }
        enriched.append(info)
    if args.json:
        print(json.dumps({"ok": True, "stores": enriched}, indent=2))
        return 0
    if not enriched:
        print("No indexed folders.")
        print("Try `hydra-llm index <path>` to create one.")
        return 0
    print(f"{'PATH':<60} {'CHUNKS':<13} EMBEDDERS")
    for s in enriched:
        if s.get("missing"):
            print(f"{s['path']:<60} {'(gone)':<13} -- (run `hydra-llm rag stores --prune`)")
            continue
        chunks = f"{s.get('code_chunks', 0)}+{s.get('prose_chunks', 0)}"
        embedders = []
        if s.get("code_embedder"):
            embedders.append(s["code_embedder"])
        if s.get("prose_embedder") and s.get("prose_embedder") != s.get("code_embedder"):
            embedders.append(s["prose_embedder"])
        print(f"{s['path']:<60} {chunks:<13} {', '.join(embedders) or '-'}")
    return 0


def cmd_index(args):
    cfg = cfg_mod.load_user_config()
    try:
        from . import rag_pipeline
    except ImportError as e:
        msg = (f"RAG pipeline import failed: {e}. "
               "Install missing deps: pip install --user pathspec numpy lancedb")
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1

    code_e = None
    prose_e = None
    if args.embedder:
        # --embedder selects one for everything (single-index mode).
        e = rag_cat_mod.find_embedder(args.embedder)
        if not e:
            print(f"error: unknown embedder id: {args.embedder}", file=sys.stderr)
            return 1
        args.single_index = True
        code_e = e
        prose_e = e
    if args.code_embedder:
        code_e = rag_cat_mod.find_embedder(args.code_embedder)
        if not code_e:
            print(f"error: unknown embedder id: {args.code_embedder}", file=sys.stderr)
            return 1
    if args.prose_embedder:
        prose_e = rag_cat_mod.find_embedder(args.prose_embedder)
        if not prose_e:
            print(f"error: unknown embedder id: {args.prose_embedder}", file=sys.stderr)
            return 1

    only_kind = None
    if args.no_code and args.no_prose:
        print("error: --no-code and --no-prose together leaves nothing to index",
              file=sys.stderr)
        return 1
    if args.no_code:
        only_kind = "prose"
    elif args.no_prose:
        only_kind = "code"

    plan = rag_pipeline.plan_index(
        Path(args.path),
        cfg=cfg,
        full_rebuild=args.full,
        extra_excludes=args.exclude,
        extra_includes=args.include,
        max_depth=args.depth,
        max_file_size_bytes=int(args.max_file_size_mb * 1024 * 1024),
        code_embedder=code_e,
        prose_embedder=prose_e,
        single_index=args.single_index,
        only_kind=only_kind,
    )

    if not plan.code_embedder and not plan.prose_embedder:
        msg = (
            "no embedders are installed. Run `hydra-llm rag list-online` to "
            "see what's available, then `hydra-llm rag download <id>` for at "
            "least one of each kind you want to index."
        )
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1

    if args.dry_run:
        if args.json:
            from dataclasses import asdict
            out = {"ok": True, "dry_run": True, "plan": {
                "root": str(plan.root),
                "code_embedder": plan.code_embedder["id"] if plan.code_embedder else None,
                "prose_embedder": plan.prose_embedder["id"] if plan.prose_embedder else None,
                "files_to_embed": [f.rel_path for f in plan.files_to_embed],
                "files_unchanged": plan.files_unchanged,
                "files_deleted": plan.files_deleted,
                "full_rebuild": plan.full_rebuild,
            }}
            print(json.dumps(out, indent=2))
        else:
            print(f"plan for {plan.root}:")
            print(f"  code embedder:  {plan.code_embedder['id'] if plan.code_embedder else '(none)'}")
            print(f"  prose embedder: {plan.prose_embedder['id'] if plan.prose_embedder else '(none)'}")
            print(f"  full rebuild:   {plan.full_rebuild}")
            print(f"  to embed:       {len(plan.files_to_embed)}")
            print(f"  unchanged:      {len(plan.files_unchanged)}")
            print(f"  deleted:        {len(plan.files_deleted)}")
        return 0

    if not args.json:
        print(f"Indexing {plan.root}")

    try:
        result = rag_pipeline.execute_plan(plan, cfg=cfg)
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1

    if args.no_register:
        rag_store_mod.unregister_store(plan.root)

    if args.json:
        print(json.dumps({
            "ok": True,
            "root": str(plan.root),
            "chunks_added": result.chunks_added,
            "chunks_removed": result.chunks_removed,
            "code_chunks_total": result.code_chunks_total,
            "prose_chunks_total": result.prose_chunks_total,
            "elapsed_seconds": round(result.elapsed_seconds, 2),
        }, indent=2))
    else:
        print(f"\nindex updated:")
        print(f"  chunks added:   {result.chunks_added}")
        if result.chunks_removed:
            print(f"  chunks removed: {result.chunks_removed}")
        print(f"  code chunks:    {result.code_chunks_total}")
        print(f"  prose chunks:   {result.prose_chunks_total}")
        print(f"  elapsed:        {result.elapsed_seconds:.1f}s")
        print()
        print(f"Try: hydra-llm query \"<text>\" --in {plan.root}")
        print(f"     hydra-llm chat <model> --rag {plan.root}")
    return 0


def cmd_query(args):
    cfg = cfg_mod.load_user_config()
    try:
        from . import rag_pipeline
    except ImportError as e:
        msg = (f"RAG pipeline import failed: {e}. "
               "Install missing deps: pip install --user pathspec numpy lancedb")
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(f"error: {msg}", file=sys.stderr)
        return 1
    try:
        results = rag_pipeline.retrieve(
            Path(args.in_path),
            args.text,
            top_k=args.top_k,
            cfg=cfg,
            code_only=args.code_only,
            prose_only=args.prose_only,
        )
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, "results": results}, indent=2))
        return 0
    if not results:
        print("(no results)")
        return 0
    print(f"Top {len(results)} results from {Path(args.in_path).resolve()}:")
    for i, r in enumerate(results, 1):
        kinds = ",".join(r.get("kinds", []))
        score = r.get("rrf", 0.0)
        line_range = f"{r.get('line_start')}-{r.get('line_end')}"
        print(f"\n{i}. [rrf {score:.4f}, {kinds}]  {r.get('rel_path')}:{line_range}")
        snippet = (r.get("text") or "").strip().splitlines()
        for line in snippet[:8]:
            print(f"     {line[:100]}")
        if len(snippet) > 8:
            print(f"     ... ({len(snippet) - 8} more lines)")
    return 0


def cmd_config_path(args):
    print(paths.CONFIG_DIR)
    return 0


def cmd_config(args):
    """Show or change per-alias server-launch overrides.

    Layered resolution: catalog defaults -> config.yaml -> per-alias
    override file. This command writes layer 4 only. When a running
    container would need a different launch command after the change,
    we offer to restart it."""
    from . import server_settings as ss

    cfg = cfg_mod.load_user_config()
    catalog, _ = cfg_mod.load_catalog()
    entry = next((m for m in catalog if m["id"] == args.alias), None)
    if entry is None:
        print(f"unknown alias: {args.alias}", file=sys.stderr)
        return 2

    # Reset cases (key may be the literal "reset" or a key name).
    if args.key == "reset":
        target = args.value  # optional specific key to reset
        if target and target not in ss.KNOWN_KEYS:
            print(f"unknown key: {target}; known: {sorted(ss.KNOWN_KEYS)}",
                  file=sys.stderr)
            return 2
        before = ss.resolve(args.alias, entry, cfg)
        ss.reset(args.alias, target)
        after = ss.resolve(args.alias, entry, cfg)
        return _config_post_change(args, before, after, args.alias)

    # Read forms: no key (show all) or just a key (show one).
    if args.key is None:
        eff = ss.resolve(args.alias, entry, cfg)
        prov = eff.pop("_provenance", {})
        if not eff:
            print(f"{args.alias}: no settings (using llama-server defaults)")
            return 0
        print(f"{args.alias}:")
        for k in sorted(eff):
            origin = prov.get(k, "?")
            print(f"  {k:<22} {eff[k]!r}  [{origin}]")
        ovr = ss.load_overrides(args.alias)
        if ovr:
            print(f"\noverride file: {ss._override_path(args.alias)}")
        return 0
    if args.key not in ss.KNOWN_KEYS:
        print(f"unknown key: {args.key}; known: {sorted(ss.KNOWN_KEYS)}",
              file=sys.stderr)
        return 2
    if args.value is None:
        eff = ss.resolve(args.alias, entry, cfg)
        if args.key in eff:
            origin = eff.get("_provenance", {}).get(args.key, "?")
            print(f"{args.key} = {eff[args.key]!r}  [{origin}]")
        else:
            print(f"{args.key} = (unset; llama-server default applies)")
        return 0

    # Write form. Coerce simple types: integers stay integers; the
    # literal strings "true"/"false" become bools; bare JSON wins for
    # extra_args (a list) and chat_template_kwargs (a dict).
    raw = args.value
    coerced = raw
    if args.key == "extra_args":
        try:
            coerced = json.loads(raw)
        except json.JSONDecodeError:
            print("extra_args expects a JSON array, e.g. '[\"--ctx-size\",\"32768\"]'",
                  file=sys.stderr)
            return 2
        if not isinstance(coerced, list):
            print("extra_args must be a JSON array", file=sys.stderr)
            return 2
    elif args.key == "chat_template_kwargs":
        try:
            coerced = json.loads(raw)
        except json.JSONDecodeError:
            print("chat_template_kwargs expects a JSON object, e.g. '{\"enable_thinking\":false}'",
                  file=sys.stderr)
            return 2
        if not isinstance(coerced, dict):
            print("chat_template_kwargs must be a JSON object", file=sys.stderr)
            return 2
    elif args.key == "predict":
        # Accept "uncapped"/"off" as strings; integer otherwise.
        if raw not in ("uncapped", "off"):
            try:
                coerced = int(raw)
            except ValueError:
                print("predict expects 'uncapped', 'off', or a positive integer",
                      file=sys.stderr)
                return 2
    elif args.key == "reasoning_format":
        if raw not in ("none", "deepseek", "hide", "off"):
            print("reasoning_format expects one of: none | deepseek | hide | off",
                  file=sys.stderr)
            return 2

    before = ss.resolve(args.alias, entry, cfg)
    ss.set_override(args.alias, args.key, coerced)
    after = ss.resolve(args.alias, entry, cfg)
    return _config_post_change(args, before, after, args.alias)


def _config_post_change(args, before: dict, after: dict, alias: str) -> int:
    """Common tail for cmd_config: report what changed and, if the
    alias is currently running and a launch-time setting changed,
    offer to restart it."""
    from . import server_settings as ss

    changed = ss.diff_launch_relevant(before, after)
    if not changed:
        print(f"  ✓ no effective change for {alias}")
        return 0

    print(f"  ✓ {alias}: changed {', '.join(changed)}")
    cfg = cfg_mod.load_user_config()
    rows, _ = docker_driver.list_running(cfg)
    container = next((r for r in rows if r["alias"] == alias and r.get("state") == "running"), None)
    if container is None:
        print("    (alias is not running; new value applies on next start)")
        return 0

    if args.no_restart:
        print("    (alias is running; --no-restart given, value applies on next start)")
        return 0

    do_restart = args.restart
    if not do_restart:
        try:
            ans = input("    alias is running. restart it now? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        do_restart = ans in ("y", "yes")
    if not do_restart:
        print("    skipped restart; value applies on next start")
        return 0

    catalog, _ = cfg_mod.load_catalog()
    entry = next((m for m in catalog if m["id"] == alias), None)
    if entry is None:
        print(f"    can't restart: alias {alias!r} not in catalog anymore",
              file=sys.stderr)
        return 1
    print(f"    restarting {alias}...")
    docker_driver.stop(alias, cfg)
    ok, info = docker_driver.start_model(entry, cfg=cfg)
    if not ok:
        print(f"    restart failed: {info.get('error')}", file=sys.stderr)
        return 1
    print(f"    ✓ restarted on port {info.get('port')}")
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
