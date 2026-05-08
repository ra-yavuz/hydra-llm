"""First-run setup logic. Used by `hydra-llm setup` and by the .deb postinst
(which runs as the user's account post-install).

Stages:
  1. Verify Docker is installed and the user can talk to the daemon.
  2. Pick the image variant: vulkan if /dev/dri is present, else cpu.
  3. Build (or pull) the image if not already present.
  4. Download a small default model (tinyllama-1.1b, ~620 MB) so the user has
     something to chat with immediately.
  5. Optional smoke test: start the model, hit /health, stop it.

Each stage prints a one-liner so a flaky connection or interrupt is obvious.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as cfg_mod, desktop, docker_driver, downloader, hardware, paths


# Default starter model: smollm2-135m. ~100 MB, downloads in seconds, boots
# in seconds, has a clean chat template. The output quality is poor by design
# (it's a 135M-param toy), but the only job here is to prove the install works
# end to end. Real users move to bigger models from `hydra-llm list-online`.
DEFAULT_SMOKE_MODEL = "smollm2-135m"

# How many times to retry a Docker build that failed with a transient error
# (daemon restart, RPC EOF, network blip during git clone of llama.cpp).
BUILD_RETRIES = 2

# Errors that look transient and are worth retrying. Anything else fails fast.
_TRANSIENT_BUILD_HINTS = (
    "rpc error",
    "EOF",
    "context canceled",
    "connection refused",
    "i/o timeout",
    "TLS handshake timeout",
    "Could not resolve host",
    "Temporary failure in name resolution",
)


def banner(msg, prefix="==>"):
    sys.stdout.write(f"{prefix} {msg}\n")
    sys.stdout.flush()


def ok(msg):
    sys.stdout.write(f"    ok  {msg}\n")
    sys.stdout.flush()


def warn(msg):
    sys.stdout.write(f"  warn  {msg}\n")
    sys.stdout.flush()


def fail(msg, hint=None):
    sys.stderr.write(f"  fail  {msg}\n")
    if hint:
        sys.stderr.write(f"        hint: {hint}\n")
    sys.stderr.flush()


def step_check_docker() -> bool:
    banner("Checking Docker")
    if not docker_driver.docker_available():
        fail("docker is not installed",
             "see https://docs.docker.com/engine/install/ then re-run `hydra-llm setup`")
        return False
    try:
        out = subprocess.run(["docker", "info"], capture_output=True, text=True,
                             timeout=10, check=True)
    except subprocess.CalledProcessError as e:
        fail("docker daemon is not reachable",
             "you may need to add yourself to the 'docker' group: "
             "sudo usermod -aG docker $USER, then log out/in")
        return False
    except FileNotFoundError:
        fail("docker not on PATH")
        return False
    ok("docker daemon is reachable")
    return True


def step_pick_image_variant() -> str:
    banner("Selecting image variant")
    if Path("/dev/dri").exists():
        ok("found /dev/dri, will use the Vulkan image (GPU acceleration)")
        return "vulkan"
    ok("no /dev/dri, will use the CPU image")
    return "cpu"


def step_build_image(variant: str, source_dir: Path = None,
                     force: bool = False, llama_ref: str = "master") -> bool:
    """Builds (locally) the requested image.

    By default this is idempotent: if the image already exists locally we
    return without rebuilding. Pass force=True to rebuild against current
    llama.cpp master (used by `hydra-llm engine rebuild` to refresh stale
    images). The Dockerfile pulls llama.cpp from `llama_ref` (default
    master); overriding this lets you pin a specific commit or tag.

    source_dir is the path to the hydra-llm source tree containing docker/.
    Defaults to the standard install location, with a dev-tree fallback.
    """
    banner(f"Preparing image hydra-llm/llama-server:{variant}")
    tag = f"hydra-llm/llama-server:{variant}"
    # Already built? Skip unless forced.
    if not force:
        try:
            out = subprocess.run(
                ["docker", "image", "inspect", tag],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                ok(f"image already present")
                return True
        except subprocess.SubprocessError:
            pass

    # Find the Dockerfile.
    if source_dir is None:
        candidates = []
        env_dir = os.environ.get("HYDRA_LLM_DOCKER_DIR")
        if env_dir:
            candidates.append(Path(env_dir))
        env_share = os.environ.get("HYDRA_LLM_SHARE")
        if env_share:
            candidates.append(Path(env_share) / "docker")
        candidates.extend([
            Path("/usr/share/hydra-llm/docker"),
            Path("/usr/local/share/hydra-llm/docker"),
            Path(__file__).resolve().parent.parent.parent / "docker",
        ])
        for cand in candidates:
            if (cand / f"Dockerfile.{variant}").is_file():
                source_dir = cand
                break
    if not source_dir or not (source_dir / f"Dockerfile.{variant}").is_file():
        fail(f"could not find Dockerfile.{variant}",
             "is hydra-llm installed correctly?")
        return False

    sys.stdout.write(f"        building image, this can take a few minutes...\n")
    sys.stdout.flush()

    last_err = ""
    for attempt in range(1, BUILD_RETRIES + 2):  # 1 initial + BUILD_RETRIES retries
        try:
            # Capture stderr so we can decide whether to retry; tee to user as well.
            proc = subprocess.run(
                ["docker", "build",
                 "-f", str(source_dir / f"Dockerfile.{variant}"),
                 "-t", tag,
                 str(source_dir)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False,
            )
            for line in (proc.stdout or "").splitlines():
                sys.stdout.write(f"          {line}\n")
            sys.stdout.flush()
            if proc.returncode == 0:
                ok("image built")
                return True
            last_err = (proc.stdout or "")[-2000:]
        except FileNotFoundError:
            fail("docker not found on PATH (it was earlier; did Docker get uninstalled?)")
            return False

        # Decide whether to retry.
        is_transient = any(hint in last_err for hint in _TRANSIENT_BUILD_HINTS)
        if attempt > BUILD_RETRIES:
            break
        if not is_transient:
            warn(f"build failed with a non-transient error; not retrying")
            break
        warn(f"build attempt {attempt} hit a transient error; retrying")

    fail(f"docker build failed for variant '{variant}'",
         "re-run later with: hydra-llm setup\n"
         "        if the error persists, try: hydra-llm setup --image cpu")
    return False


def step_download_default_model(model_id: str = DEFAULT_SMOKE_MODEL) -> bool:
    banner(f"Downloading starter model: {model_id}")
    cfg = cfg_mod.load_user_config()
    catalog, _ = cfg_mod.load_catalog()
    entry = next((m for m in catalog if m["id"] == model_id), None)
    if not entry:
        fail(f"catalog does not contain '{model_id}'")
        return False
    if downloader.is_downloaded(entry, cfg):
        ok(f"already downloaded ({entry['filename']})")
        return True
    sys.stdout.write(f"        ~{entry.get('size_gb', '?')} GB, anonymous\n")
    sys.stdout.flush()
    try:
        path = downloader.download(entry, cfg)
    except RuntimeError as e:
        fail(f"download failed: {e}")
        return False
    ok(f"saved to {path}")
    return True


def step_smoke_test(model_id: str = DEFAULT_SMOKE_MODEL) -> bool:
    banner(f"Smoke test: starting {model_id}, probing /health, stopping")
    cfg = cfg_mod.load_user_config()
    catalog, _ = cfg_mod.load_catalog()
    entry = next((m for m in catalog if m["id"] == model_id), None)
    if not entry:
        fail(f"catalog does not contain '{model_id}'")
        return False

    started_ok, info = docker_driver.start_model(entry, cfg)
    if not started_ok:
        fail(f"could not start: {info.get('error')}")
        return False
    container = info["container"]
    port = info["port"]
    sys.stdout.write(f"        container={container} port={port}\n")
    sys.stdout.flush()

    try:
        # Stage 1: /health
        for i in range(60):
            if docker_driver.probe_health(port, timeout=1.5):
                ok(f"became ready after ~{i*2} seconds")
                break
            time.sleep(2)
        else:
            fail("did not become ready within 120s")
            docker_driver.stop(model_id, cfg)
            return False

        # Stage 2: actually generate something through /v1/chat/completions.
        # This catches broken chat templates that /health doesn't see.
        import json as _json, urllib.request as _ur, urllib.error as _ue
        body = _json.dumps({
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8, "temperature": 0,
        }).encode()
        req = _ur.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                          data=body,
                          headers={"Content-Type": "application/json"})
        try:
            with _ur.urlopen(req, timeout=60) as r:
                data = _json.load(r)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                ok(f"chat endpoint replied: {content[:60].strip()!r}")
            else:
                warn("chat endpoint returned empty content (model may need more tokens, not fatal)")
        except _ue.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            fail(f"chat endpoint returned HTTP {e.code}",
                 f"server said: {err_body.strip()[:200]}\n"
                 f"        try a different model: hydra-llm setup --model gemma-2-2b")
            docker_driver.stop(model_id, cfg)
            return False
        except _ue.URLError as e:
            fail(f"could not reach chat endpoint: {e.reason}")
            docker_driver.stop(model_id, cfg)
            return False

        docker_driver.stop(model_id, cfg)
        ok("stopped cleanly")
        return True
    except KeyboardInterrupt:
        warn("interrupted, stopping the test container")
        docker_driver.stop(model_id, cfg)
        raise


def run_setup(*, build: bool = True, download: bool = True, test: bool = True,
              model_id: str = DEFAULT_SMOKE_MODEL,
              image_override: str = None,
              force_rebuild: bool = False) -> int:
    """Runs the full first-run setup. Returns 0 on full success.

    Build strategy:
      - If image_override is given ("vulkan" or "cpu"), only that image is built.
      - Otherwise, the preferred image is picked based on /dev/dri presence.
        We always also build the CPU image as a safety net so users can fall
        back to it manually with `hydra-llm config set image cpu`.
      - force_rebuild bypasses the "image already present" early-return so
        users can refresh against a newer pinned llama.cpp ref after upgrade.
    """
    paths.ensure_user_dirs()

    if not step_check_docker():
        return 1

    if image_override in ("vulkan", "cpu"):
        preferred = image_override
        also_build_cpu = (preferred != "cpu")
    else:
        preferred = step_pick_image_variant()
        # Build cpu as a fallback, unless the preferred IS cpu.
        also_build_cpu = (preferred != "cpu")

    have_working_image = False
    if build:
        if step_build_image(preferred, force=force_rebuild):
            have_working_image = True
        else:
            if preferred != "cpu":
                warn(f"falling back to CPU image since {preferred} build failed")
                if step_build_image("cpu", force=force_rebuild):
                    have_working_image = True
                    preferred = "cpu"
        if also_build_cpu and preferred != "cpu":
            # Best effort; not fatal if it fails.
            banner("Also building CPU image as a safety-net fallback")
            if not step_build_image("cpu", force=force_rebuild):
                warn("CPU fallback image build failed; only the preferred image is available")
        if not have_working_image:
            fail("could not build any usable image",
                 "open an issue with the build log; for now you can try other variants manually")
            return 1
    else:
        have_working_image = True  # user said --no-build, trust them

    if download and not step_download_default_model(model_id):
        warn("starter model not downloaded; setup considered partial")

    if test:
        if not step_smoke_test(model_id):
            warn("smoke test did not pass")
            return 1

    # Install the user-level autokill timer so idle chat-model containers
    # get reaped without the user remembering. Best-effort, never fatal.
    try:
        from . import reaper_unit
        st = reaper_unit.status()
        if not st.get("installed"):
            ok, msg = reaper_unit.enable()
            if ok:
                sys.stdout.write("\n")
                sys.stdout.write("    autokill: enabled (idle chat models stop "
                                 "after chat_idle_ttl_seconds, default 600s).\n")
                sys.stdout.write("    disable with: hydra-llm reaper disable\n")
            else:
                warn(f"could not enable autokill timer: {msg}")
    except Exception as e:
        warn(f"autokill timer setup skipped: {e}")

    banner("Setup complete")
    sys.stdout.write(f"    image:  hydra-llm/llama-server:{preferred}\n")
    sys.stdout.write(f"\n")
    sys.stdout.write(f"    Try:\n")
    sys.stdout.write(f"      hydra-llm chat {model_id}\n")
    sys.stdout.write(f"      hydra-llm list-online\n")

    # DE-aware widget hint.
    de = desktop.detect()
    if de.get("widget_package") and not desktop.is_widget_installed(de["widget_package"]):
        sys.stdout.write(f"\n    {de['name']} detected. Install the panel widget:\n")
        sys.stdout.write(f"      sudo apt install {de['widget_package']}\n")
    elif de.get("widget_package") and desktop.is_widget_installed(de["widget_package"]):
        sys.stdout.write(f"\n    Panel widget is installed. Add it from\n")
        sys.stdout.write(f"    'Add Widgets...' on your Plasma panel.\n")
    sys.stdout.flush()
    return 0
