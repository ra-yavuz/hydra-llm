"""Docker container driver. Runs llama-server containers for catalog models."""
import json
import re
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config as cfg_mod
from . import paths


def docker_available() -> bool:
    return shutil.which("docker") is not None


def _docker_compose_args():
    """Returns the prefix args to invoke docker compose. Both v1 (docker-compose) and v2 (docker compose)."""
    if shutil.which("docker"):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def _container_name(prefix: str, alias: str) -> str:
    return f"{prefix}{alias}"


def list_running(cfg=None):
    """List all containers we manage. Returns list of dicts with alias, container, state, port, status."""
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    prefix = cfg["container_prefix"]
    if not docker_available():
        return [], "docker not installed"
    try:
        out = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"name=^{prefix}",
             "--format", "{{.Names}}\t{{.State}}\t{{.Ports}}\t{{.Status}}\t{{.CreatedAt}}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        return [], f"docker ps failed: {e}"
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, state, ports, status, created = parts
        alias = name[len(prefix):]
        port = None
        m = re.search(r":(\d+)->8081/tcp", ports)
        if m:
            port = int(m.group(1))
        rows.append({
            "alias": alias,
            "container": name,
            "state": state,
            "port": port,
            "status": status,
            "created": created,
        })
    return rows, None


def probe_health(port, timeout=0.4):
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return b"ok" in r.read(64)
    except Exception:
        return False


def annotate_health(rows):
    """Mutates rows: adds .ready (bool) by hitting /health on each port in parallel."""
    if not rows:
        return rows
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda r: probe_health(r["port"]), rows))
    for r, ok in zip(rows, results):
        r["ready"] = ok
    return rows


def _container_state(name):
    """Return the docker State.Status of `name`, or None if it doesn't exist."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def container_logs_tail(name, n=10):
    """Return the last n lines of combined stdout/stderr for the container.
    Empty string if logs aren't available."""
    try:
        r = subprocess.run(
            ["docker", "logs", "--tail", str(n), name],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout + r.stderr).rstrip()


def wait_for_ready(name, port, timeout=60.0, on_tick=None):
    """Poll the container's /health until ready, exited, or timeout.

    Returns a dict:
      {"state": "ready"|"exited"|"loading", "elapsed": float, "logs": str}

    `on_tick(elapsed)` is called once per poll loop iteration if provided,
    so the CLI can print progress dots without us depending on stdout here.
    """
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if on_tick:
            try: on_tick(elapsed)
            except Exception: pass
        # Container died?
        state = _container_state(name)
        if state in ("exited", "dead"):
            return {"state": "exited", "elapsed": elapsed,
                    "logs": container_logs_tail(name, 20)}
        # /health says ok?
        if probe_health(port, timeout=0.4):
            return {"state": "ready", "elapsed": elapsed, "logs": ""}
        if time.monotonic() >= deadline:
            return {"state": "loading", "elapsed": elapsed,
                    "logs": container_logs_tail(name, 5)}
        time.sleep(0.5)


def find_free_port(used_ports, low, high):
    for p in range(low, high + 1):
        if p not in used_ports:
            return p
    return None


def start_model(catalog_entry, cfg=None, port=None):
    """Starts a llama-server container for the given catalog entry. Returns (ok, info_dict)."""
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    if not docker_available():
        return False, {"error": "docker is not installed"}

    alias = catalog_entry["id"]
    prefix = cfg["container_prefix"]
    name = _container_name(prefix, alias)

    rows, _ = list_running(cfg)
    existing = next((r for r in rows if r["container"] == name), None)
    if existing and existing.get("state") == "running":
        return True, {"already_running": True, "container": name}
    # Stale container in 'exited'/'created' state: docker run --name would
    # collide. Remove it so we can recreate cleanly.
    if existing:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, text=True, timeout=10)

    used_ports = {r["port"] for r in rows if r["port"] and r["container"] != name}
    if port is None:
        port = catalog_entry.get("default_port")
    if not port or port in used_ports:
        port = find_free_port(used_ports, *cfg["port_range"])
        if not port:
            return False, {"error": "no free port in port_range"}

    gpu_layers = catalog_entry.get("gpu_layers", 99)
    gguf_filename = catalog_entry["filename"]
    models_dir = paths.MODELS_DIR_DEFAULT  # honor user's models_dir from cfg if set
    models_dir = cfg.get("models_dir") or str(models_dir)
    gguf_path = f"{models_dir}/{gguf_filename}"

    image = _resolve_image(cfg)

    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "-p", f"{port}:8081",
        "-v", f"{models_dir}:/models:ro",
    ]
    # Pass through GPU devices if available.
    if image == "vulkan":
        cmd += ["--device", "/dev/dri"]
    cmd += [
        image_tag(image),
        "--model", f"/models/{gguf_filename}",
        "--n-gpu-layers", str(gpu_layers),
        "--host", "0.0.0.0",
        "--port", "8081",
        "--log-disable",
    ]
    # Resolve per-alias server settings (catalog + config + per-alias
    # override layers). Driver remains tolerant of unknown keys.
    from . import server_settings
    eff = server_settings.resolve(alias, catalog_entry, cfg)
    predict = eff.get("predict")
    if predict == "uncapped":
        cmd += ["--predict", "-1"]
    elif isinstance(predict, int):
        cmd += ["--predict", str(predict)]
    elif isinstance(predict, str) and predict not in ("off", ""):
        try:
            cmd += ["--predict", str(int(predict))]
        except ValueError:
            pass
    rf = eff.get("reasoning_format")
    rf_map = {"none": "none", "deepseek": "deepseek", "hide": "auto"}
    if isinstance(rf, str) and rf in rf_map:
        cmd += ["--reasoning-format", rf_map[rf]]
    # chat_template_kwargs: passed as a single JSON arg to llama-server's
    # --chat-template-kwargs flag. Used to disable thinking mode on
    # gemma-4 and similar runtime-toggleable reasoning models.
    ctk = eff.get("chat_template_kwargs")
    if isinstance(ctk, dict) and ctk:
        import json as _json
        cmd += ["--chat-template-kwargs", _json.dumps(ctk)]
    extra = eff.get("extra_args")
    if isinstance(extra, list) and extra:
        cmd += [str(x) for x in extra]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        return False, {"error": f"docker run failed: {e.stderr.strip()}"}
    return True, {"container": name, "port": port, "image": image}


def stop(alias: str, cfg=None):
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    name = _container_name(cfg["container_prefix"], alias)
    try:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, text=True, check=True, timeout=15)
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()
    return True, name


def stop_all(cfg=None):
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    rows, err = list_running(cfg)
    if err:
        return False, err, []
    if not rows:
        return True, None, []
    names = [r["container"] for r in rows]
    try:
        subprocess.run(["docker", "rm", "-f", *names],
                       capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip(), []
    return True, None, names


def _resolve_image(cfg) -> str:
    """Returns 'vulkan' or 'cpu'. 'auto' picks vulkan if /dev/dri exists, otherwise cpu."""
    val = cfg.get("image", "auto")
    if val in ("vulkan", "cpu"):
        return val
    # auto
    from pathlib import Path as _P
    return "vulkan" if _P("/dev/dri").exists() else "cpu"


def image_tag(variant: str) -> str:
    """Local image tag we expect to exist after install/build. The install script builds these."""
    return f"hydra-llm/llama-server:{variant}"


# --- embedder sidecars ----------------------------------------------------------
#
# Embedders are llama-server containers run with --embeddings, on a dedicated
# port range (default 19080..19099) so they coexist with chat-model containers
# (default 18080..18099). Treated as sidecars rather than catalog-listed chat
# models: they don't show up in `hydra-llm list` or `hydra-llm status`, only
# under `hydra-llm rag list` / `hydra-llm rag info`.

def list_running_embedders(cfg=None):
    """List embedder sidecars hydra manages. Same shape as list_running()."""
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    prefix = cfg.get("embedder_container_prefix", "hydra-embed-")
    if not docker_available():
        return [], "docker not installed"
    try:
        out = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"name=^{prefix}",
             "--format", "{{.Names}}\t{{.State}}\t{{.Ports}}\t{{.Status}}\t{{.CreatedAt}}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        return [], f"docker ps failed: {e}"
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, state, ports, status, created = parts
        alias = name[len(prefix):]
        port = None
        m = re.search(r":(\d+)->8081/tcp", ports)
        if m:
            port = int(m.group(1))
        rows.append({
            "alias": alias,
            "container": name,
            "state": state,
            "port": port,
            "status": status,
            "created": created,
        })
    return rows, None


def start_embedder(embedder_entry, cfg=None, port=None):
    """Start a llama-server container in --embeddings mode for the given
    embedder catalog entry. Idempotent: returns the existing container if one
    is already running for this id.
    """
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    if not docker_available():
        return False, {"error": "docker is not installed"}

    alias = embedder_entry["id"]
    prefix = cfg.get("embedder_container_prefix", "hydra-embed-")
    name = _container_name(prefix, alias)

    rows, _ = list_running_embedders(cfg)
    existing = next((r for r in rows if r["container"] == name), None)
    if existing and existing.get("state") == "running":
        return True, {"already_running": True, "container": name,
                      "port": existing.get("port")}
    if existing:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, text=True, timeout=10)

    used_ports = {r["port"] for r in rows if r["port"] and r["container"] != name}
    port_range = cfg.get("embedder_port_range", [19080, 19099])
    if port is None:
        port = embedder_entry.get("default_port")
    if not port or port in used_ports:
        port = find_free_port(used_ports, *port_range)
        if not port:
            return False, {"error": "no free port in embedder_port_range"}

    gpu_layers = embedder_entry.get("gpu_layers", 99)
    gguf_filename = embedder_entry["filename"]
    embedders_dir = cfg.get("embedders_dir") or str(paths.EMBEDDERS_DIR_DEFAULT)

    image = _resolve_image(cfg)
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "-p", f"{port}:8081",
        "-v", f"{embedders_dir}:/models:ro",
    ]
    if image == "vulkan":
        cmd += ["--device", "/dev/dri"]
    cmd += [
        image_tag(image),
        "--model", f"/models/{gguf_filename}",
        "--n-gpu-layers", str(gpu_layers),
        "--host", "0.0.0.0",
        "--port", "8081",
        "--log-disable",
        "--embeddings",
    ]
    pooling = embedder_entry.get("pooling")
    if pooling and pooling != "none":
        cmd += ["--pooling", pooling]
    # llama-server's default --ubatch-size is 512, which rejects single
    # documents longer than that. The chunker emits chunks up to ~1500
    # characters, which can be ~700 tokens for code and well over 512 for
    # multilingual prose. Bump batch sizes to 2048 (or the embedder's
    # max_tokens, whichever is smaller).
    max_tokens = embedder_entry.get("max_tokens") or 0
    ubatch = 2048
    if max_tokens and max_tokens < ubatch:
        ubatch = max_tokens
    cmd += ["--ubatch-size", str(ubatch), "--batch-size", str(ubatch)]
    # Carry max_tokens into context size if supplied.
    if isinstance(max_tokens, int) and max_tokens > 0:
        cmd += ["--ctx-size", str(max_tokens)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        return False, {"error": f"docker run failed: {e.stderr.strip()}"}
    return True, {"container": name, "port": port, "image": image}


def stop_embedder(alias: str, cfg=None):
    """Stop and remove the embedder sidecar for the given embedder id."""
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    prefix = cfg.get("embedder_container_prefix", "hydra-embed-")
    name = _container_name(prefix, alias)
    try:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, text=True, check=True, timeout=15)
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()
    return True, name


def stop_all_embedders(cfg=None):
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    rows, err = list_running_embedders(cfg)
    if err:
        return False, err, []
    if not rows:
        return True, None, []
    names = [r["container"] for r in rows]
    try:
        subprocess.run(["docker", "rm", "-f", *names],
                       capture_output=True, text=True, check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip(), []
    return True, None, names


def ensure_embedder_running(embedder_entry, cfg=None, wait_timeout=60.0):
    """Idempotent helper: start the embedder if it isn't running, then wait
    for /health to come up. Returns (ok, info) where info has .container,
    .port, and on failure .error and .logs.
    """
    if cfg is None:
        cfg = cfg_mod.load_user_config()
    ok, info = start_embedder(embedder_entry, cfg)
    if not ok:
        return False, info
    name = info["container"]
    port = info["port"]
    if info.get("already_running") and probe_health(port):
        return True, info
    res = wait_for_ready(name, port, timeout=wait_timeout)
    if res["state"] == "ready":
        return True, info
    return False, {"error": f"embedder did not become ready ({res['state']})",
                   "container": name, "port": port, "logs": res.get("logs", "")}
