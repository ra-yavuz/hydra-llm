"""Docker container driver. Runs llama-server containers for catalog models."""
import json
import re
import shutil
import subprocess
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
    # Default cap on generated tokens for clients that omit max_tokens.
    # "off" means "don't pass --predict; let llama-server use its built-in 128".
    predict = cfg.get("predict")
    if predict == "uncapped":
        cmd += ["--predict", "-1"]
    elif isinstance(predict, int):
        cmd += ["--predict", str(predict)]
    elif isinstance(predict, str) and predict not in ("off", ""):
        try:
            cmd += ["--predict", str(int(predict))]
        except ValueError:
            pass  # Unrecognized value: silently skip rather than fail to start.
    extra = catalog_entry.get("extra_args", [])
    if isinstance(extra, list):
        cmd += extra

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
