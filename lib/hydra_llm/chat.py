"""Streaming chat client with persistent sessions, color UI, and live boot logs."""
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import overrides as overrides_mod, paths
from .personas import Persona


# ANSI helpers. Disabled when stdout is not a TTY so piped output stays clean.
def _supports_color() -> bool:
    return sys.stdout.isatty()


_ANSI = {
    "reset":  "\033[0m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
    "red":    "\033[31m",
    "green":  "\033[32m",
    "yellow": "\033[33m",
    "blue":   "\033[34m",
    "magenta":"\033[35m",
    "cyan":   "\033[36m",
}


def color(s: str, *names: str) -> str:
    if not _supports_color():
        return s
    prefix = "".join(_ANSI[n] for n in names if n in _ANSI)
    return f"{prefix}{s}{_ANSI['reset']}"


def session_path(name: str) -> Path:
    paths.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return paths.SESSIONS_DIR / f"{name}.json"


def load_session_from(path: Path, system_prompt: str):
    if path.is_file():
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return [{"role": "system", "content": system_prompt}] if system_prompt else []


def save_session_to(path: Path, messages):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(messages, f, indent=2)


def load_session(name: str, system_prompt: str):
    return load_session_from(session_path(name), system_prompt)


def save_session(name: str, messages):
    save_session_to(session_path(name), messages)


def _spinner(stop_evt: threading.Event, prefix: str):
    """Tiny braille spinner shown while waiting for the model's first token."""
    if not _supports_color():
        return
    frames = "⠇⠋⠙⠸⢰⢠⢤⠦"
    i = 0
    while not stop_evt.is_set():
        sys.stdout.write(f"\r{prefix} {color(frames[i % len(frames)], 'cyan')} thinking... ")
        sys.stdout.flush()
        i += 1
        stop_evt.wait(0.08)
    sys.stdout.write(f"\r\033[2K{prefix} ")
    sys.stdout.flush()


def _probe_health_once(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=1.0) as r:
            return b"ok" in r.read(64)
    except Exception:
        return False


def _stream_logs_until_ready(container_name: str, base_url: str, deadline: float) -> bool:
    """Tail container stdout/stderr live in this terminal until /health is ok or
    the deadline / container exit aborts. Returns True if ready, False otherwise.

    On failure, surfaces the inspector verdict so the user sees *why* the
    container died (OOMKilled, non-zero exit, etc.) rather than just "exited".
    """
    sys.stdout.write(color(f"[streaming startup logs from {container_name}]\n", "dim"))
    sys.stdout.flush()
    tail = subprocess.Popen(
        ["docker", "logs", "-f", "--tail", "50", container_name],
        stdout=None, stderr=subprocess.STDOUT,
    )
    ready = False
    try:
        while time.time() < deadline:
            if tail.poll() is not None:
                # docker logs -f exits when the container is removed.
                sys.stdout.write(color("\n[container exited before becoming healthy]\n", "red"))
                _explain_container_exit(container_name)
                return False
            if _probe_health_once(base_url):
                ready = True
                break
            time.sleep(1.0)
    finally:
        if tail.poll() is None:
            tail.terminate()
            try:
                tail.wait(timeout=2)
            except subprocess.TimeoutExpired:
                tail.kill()
    return ready


def _explain_container_exit(container_name: str) -> None:
    """Inspect a (possibly-removed) container and tell the user *why* it died.

    Looks for the canonical signals: OOMKilled, non-zero exit code, killed
    by SIGKILL/9, etc. Falls back to the last 30 log lines so the user
    has something concrete to act on. Fail-soft: never raises.
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|"
             "{{.State.Error}}|{{.State.FinishedAt}}",
             container_name],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode == 0 and r.stdout.strip():
            status, code, oom, err, finished = (r.stdout.strip().split("|", 4) + [""] * 5)[:5]
            parts = [f"status={status}", f"exit={code}"]
            if oom == "true":
                parts.append("OOMKilled=YES (kernel killed the container; reduce gpu_layers "
                             "or pick a smaller model for this hardware)")
            if err:
                parts.append(f"docker_error={err}")
            sys.stderr.write(color("  " + ", ".join(parts) + "\n", "red"))
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["docker", "logs", "--tail", "30", container_name],
            capture_output=True, text=True, timeout=4,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if out.strip():
            sys.stderr.write(color("  --- last 30 log lines ---\n", "dim"))
            sys.stderr.write(out.rstrip() + "\n")
    except Exception:
        pass


def stream_chat(
    base_url: str,
    messages,
    *,
    sampling_params: dict,
    show_thoughts: bool = True,
    out=sys.stdout,
):
    """Sends a streaming chat request. Writes content to `out`, returns
    (full_content, full_thoughts).
    """
    payload = {
        "model": "x",
        "messages": messages,
        "stream": True,
    }
    # Map our keys to llama-server / OpenAI compatible fields. -1 sentinels mean omit.
    for k, v in sampling_params.items():
        if k in ("max_tokens", "seed") and v == -1:
            continue
        payload[k] = v

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    full = []
    thoughts = []
    in_thought_block = False
    spin_stop = threading.Event()
    spin_thread = threading.Thread(
        target=_spinner, args=(spin_stop, color("model>", "bold", "magenta")),
        daemon=True,
    )
    spin_thread.start()
    first_token = False

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                delta = data.get("choices", [{}])[0].get("delta", {})

                rc = delta.get("reasoning_content")
                if rc:
                    thoughts.append(rc)
                    if show_thoughts:
                        if not first_token:
                            spin_stop.set()
                            spin_thread.join(timeout=0.5)
                            first_token = True
                        if not in_thought_block:
                            out.write(color("\n[thinking] ", "dim"))
                            in_thought_block = True
                        out.write(color(rc, "dim"))
                        out.flush()

                content = delta.get("content")
                if content:
                    if not first_token:
                        spin_stop.set()
                        spin_thread.join(timeout=0.5)
                        first_token = True
                    if in_thought_block:
                        out.write("\n\n")
                        in_thought_block = False
                    full.append(content)
                    out.write(content)
                    out.flush()
    except urllib.error.HTTPError as e:
        spin_stop.set()
        body_text = e.read().decode("utf-8", errors="replace")
        out.write(color(f"\n[error] HTTP {e.code} {e.reason}: {body_text[:200]}\n", "red"))
    except urllib.error.URLError as e:
        spin_stop.set()
        out.write(color(f"\n[error] connection failed: {e.reason}\n", "red"))
    except KeyboardInterrupt:
        spin_stop.set()
        out.write(color("\n(interrupted)\n", "yellow"))
    finally:
        if not spin_stop.is_set():
            spin_stop.set()
            spin_thread.join(timeout=0.5)
    if in_thought_block:
        out.write("\n")
    out.write("\n")
    out.flush()
    return "".join(full), "".join(thoughts)


def interactive_chat(
    base_url: str,
    *,
    persona: Optional[Persona] = None,
    alias: Optional[str] = None,
    catalog_entry: Optional[dict] = None,
    session_name: str = "default",
    session_file: Optional[Path] = None,
    show_thoughts: bool = True,
    cli_overrides: Optional[dict] = None,
    container_name: Optional[str] = None,
    rag_config=None,  # rag_chat.RagConfig | None
    cli_system_prompt: Optional[str] = None,
    cli_system_source: Optional[str] = None,
):
    """Interactive REPL.

    Resolution order for the system prompt:
      cli_system_prompt (--system / --system-file) > persona > per-alias
      prompt > inline catalog system_prompt > none

    Resolution order for sampling params:
      cli_overrides > persona settings > per-alias params > inline catalog params > defaults

    `container_name` enables live log streaming during the /health wait when the
    server is not yet responsive (caller has just started it).
    """
    # Wait for /health, optionally tailing container logs in the meantime.
    deadline = time.time() + 300
    healthy = _probe_health_once(base_url)
    if not healthy:
        if container_name:
            healthy = _stream_logs_until_ready(container_name, base_url, deadline)
        else:
            sys.stdout.write(color("waiting for /health...", "dim") + "\n")
            sys.stdout.flush()
            while time.time() < deadline:
                if _probe_health_once(base_url):
                    healthy = True
                    break
                time.sleep(1.0)
        if not healthy:
            sys.stdout.write(color("[server did not become healthy]\n", "red"))
            if container_name:
                _explain_container_exit(container_name)
            return

    # Resolve effective system prompt and params.
    if cli_system_prompt is not None:
        sys_prompt = cli_system_prompt
        prompt_source = cli_system_source or "--system"
    elif persona:
        sys_prompt = persona.system_prompt
        prompt_source = f"persona '{persona.name}'"
    else:
        info = overrides_mod.resolve_prompt(alias or "", catalog_entry)
        sys_prompt = info["content"]
        prompt_source = info["source"]

    pset = overrides_mod.resolve_params(alias or "", catalog_entry)
    chat_params = dict(pset["params"])
    if persona:
        if persona.temperature is not None:
            chat_params["temperature"] = float(persona.temperature)
        if persona.max_tokens is not None:
            chat_params["max_tokens"] = int(persona.max_tokens)
    for k, v in (cli_overrides or {}).items():
        if v is not None and k in overrides_mod.PARAM_TYPES:
            chat_params[k] = overrides_mod.PARAM_TYPES[k](v)

    print(color("==", "bold", "cyan"),
          color(f"hydra-llm chat: {alias or 'session'}", "bold", "cyan"),
          color("==", "bold", "cyan"))
    if sys_prompt:
        print(color(f"[system prompt: {len(sys_prompt)} chars from {prompt_source}]", "dim"))
    over_keys = pset["overrides"]
    if over_keys:
        bits = ", ".join(f"{k}={chat_params[k]}({src})" for k, src in over_keys.items())
        print(color(f"[per-alias params: {bits}]", "dim"))
    if rag_config is not None and rag_config.enabled:
        print(color(f"[rag: {rag_config.scope_label()} - top {rag_config.top_k}]", "dim"))
    cmds = "/reset, /quit, /set <key> <value>, /params, /thoughts on|off"
    if rag_config is not None:
        cmds += ", /rag on|off, /rag-show on|off, /rag-chunks on|off, /rag <text>"
    cmds += ", /help"
    print(color(f"commands: {cmds}", "dim"))

    effective_session_path = session_file if session_file else session_path(session_name)
    messages = load_session_from(effective_session_path, sys_prompt)
    initial_message_count = len(messages)
    print(color(f"[session: {effective_session_path}]", "dim"))
    # If the user has changed the prompt since the session was saved, reflect that.
    if messages and messages[0].get("role") == "system" and sys_prompt:
        if messages[0]["content"] != sys_prompt:
            messages[0]["content"] = sys_prompt
    elif sys_prompt and (not messages or messages[0].get("role") != "system"):
        messages.insert(0, {"role": "system", "content": sys_prompt})

    while True:
        try:
            user = input(color("\nyou> ", "bold", "green"))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        u = user.strip()
        if not u:
            continue
        if u in ("/quit", "/exit"):
            break
        if u == "/reset":
            messages = [m for m in messages if m["role"] == "system"]
            print(color("(history cleared)", "yellow"))
            continue
        if u in ("/help", "/?"):
            print("/reset             clear chat history (keeps system prompt)")
            print("/quit, /exit       leave the chat")
            print("/params            show current sampling params")
            print(f"/set <key> <val>   change a param for this session only")
            print(f"                   (keys: {', '.join(overrides_mod.PARAM_DEFAULTS)})")
            print("/thoughts on|off   show or hide reasoning_content blocks")
            if rag_config is not None:
                print("/rag on|off        toggle retrieval for this session")
                print("/rag-show on|off   show or hide the [rag: N chunks] line")
                print("/rag-chunks on|off show or hide retrieved chunk text in the terminal")
                print("/rag <text>        one-off retrieval, prints hits without sending to model")
            continue
        if u == "/params":
            for k in overrides_mod.PARAM_DEFAULTS:
                src = over_keys.get(k, "default")
                print(f"  {k:>16} = {chat_params[k]} ({src})")
            continue
        if u.startswith("/set"):
            parts = u.split(None, 2)
            if len(parts) != 3:
                print("usage: /set <key> <value>")
                continue
            _, key, raw = parts
            if key not in overrides_mod.PARAM_TYPES:
                print(f"unknown key '{key}'. valid: {', '.join(overrides_mod.PARAM_DEFAULTS)}")
                continue
            try:
                chat_params[key] = overrides_mod.PARAM_TYPES[key](raw)
                over_keys[key] = "session"
                print(color(f"(session) {key} = {chat_params[key]}", "yellow"))
            except ValueError:
                print(color(f"can't parse '{raw}' as {overrides_mod.PARAM_TYPES[key].__name__}", "red"))
            continue
        if u.startswith("/thoughts"):
            parts = u.split()
            if len(parts) == 2 and parts[1] in ("on", "off"):
                show_thoughts = parts[1] == "on"
                print(color(f"[thoughts: {'on' if show_thoughts else 'off'}]", "yellow"))
            else:
                print("usage: /thoughts on|off")
            continue
        if rag_config is not None and u.startswith("/rag"):
            parts = u.split(maxsplit=1)
            verb = parts[0]
            rest = parts[1].strip() if len(parts) > 1 else ""
            if verb == "/rag" and rest in ("on", "off"):
                rag_config.enabled = (rest == "on")
                print(color(f"[rag: {'on' if rag_config.enabled else 'off'}]", "yellow"))
                continue
            if verb == "/rag-show" and rest in ("on", "off"):
                rag_config.show_attached = (rest == "on")
                print(color(f"[rag-show: {'on' if rag_config.show_attached else 'off'}]", "yellow"))
                continue
            if verb == "/rag-chunks" and rest in ("on", "off"):
                rag_config.show_chunks = (rest == "on")
                print(color(f"[rag-chunks: {'on' if rag_config.show_chunks else 'off'}]", "yellow"))
                continue
            if verb == "/rag" and rest:
                # One-off retrieval, no model call.
                from . import rag_chat
                _, hits = rag_chat.attach_context(rag_config, rest)
                if not hits:
                    print(color("(no results)", "dim"))
                else:
                    for i, h in enumerate(hits, 1):
                        store = h.get("store_path")
                        loc = h.get("rel_path", "?")
                        line_range = f"{h.get('line_start')}-{h.get('line_end')}"
                        location = f"{store}/{loc}:{line_range}" if store else f"{loc}:{line_range}"
                        print(color(f"\n{i}. {location}", "bold"))
                        for line in (h.get("text") or "").rstrip().splitlines()[:8]:
                            print(f"   {line[:100]}")
                continue
            print("usage: /rag on|off, /rag-show on|off, /rag-chunks on|off, /rag <text>")
            continue

        # Augment with retrieved context if RAG is on. Persist the original
        # user text into history; only the per-request copy carries context,
        # so saved sessions stay small and re-augment fresh each turn.
        request_messages = list(messages)
        rag_hits: list[dict] = []
        if rag_config is not None and rag_config.enabled:
            from . import rag_chat
            augmented, rag_hits = rag_chat.attach_context(rag_config, user)
            if rag_config.show_attached:
                print(color(rag_chat.render_attached_line(rag_hits), "dim"))
            if rag_config.show_chunks and rag_hits:
                for h in rag_hits:
                    store = h.get("store_path")
                    loc = h.get("rel_path", "?")
                    line_range = f"{h.get('line_start')}-{h.get('line_end')}"
                    location = f"{store}/{loc}:{line_range}" if store else f"{loc}:{line_range}"
                    print(color(f"  ── {location}", "dim"))
            request_messages.append({"role": "user", "content": augmented})
        else:
            request_messages.append({"role": "user", "content": user})

        # Persist original user text (not augmented) so resumes stay clean.
        messages.append({"role": "user", "content": user})

        sys.stdout.write("\n" + color("model> ", "bold", "magenta"))
        full, _t = stream_chat(
            base_url, request_messages,
            sampling_params=chat_params,
            show_thoughts=show_thoughts,
        )
        if full:
            messages.append({"role": "assistant", "content": full})
            save_session_to(effective_session_path, messages)

    if len(messages) > initial_message_count:
        save_session_to(effective_session_path, messages)
        print(color("[session saved]", "dim"))
