"""Detect CPU, RAM, GPU, VRAM. Vendor-agnostic where possible."""
import os
import shutil
import subprocess
import time
from pathlib import Path


def cpu_info():
    cores = os.cpu_count() or 1
    model = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"cores": cores, "model": model}


def cpu_pct(sample_seconds: float = 0.2) -> int:
    """Quick CPU% sample. Two reads of /proc/stat with a small sleep between."""
    try:
        t0, i0 = _proc_stat_total_idle()
        time.sleep(sample_seconds)
        t1, i1 = _proc_stat_total_idle()
    except OSError:
        return 0
    dt = max(1, t1 - t0)
    di = i1 - i0
    return max(0, min(100, int(round(100 * (dt - di) / dt))))


def _proc_stat_total_idle():
    with open("/proc/stat") as f:
        line = f.readline()
    parts = line.split()
    nums = [int(x) for x in parts[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums), idle


def ram_info():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                v = v.strip().split()
                if v:
                    info[k] = int(v[0])
    except OSError:
        return {"total_mb": 0, "available_mb": 0, "used_mb": 0}
    total_kb = info.get("MemTotal", 0)
    avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
    return {
        "total_mb": total_kb // 1024,
        "available_mb": avail_kb // 1024,
        "used_mb": max(0, (total_kb - avail_kb)) // 1024,
    }


def gpu_info():
    """Returns a list of GPU dicts. Empty list if no compatible GPU detected."""
    cards = []
    # AMD via sysfs.
    for card in sorted(Path("/sys/class/drm").glob("card*")):
        if "-" in card.name:
            continue
        dev = card / "device"
        busy = dev / "gpu_busy_percent"
        used = dev / "mem_info_vram_used"
        total = dev / "mem_info_vram_total"
        if not (busy.exists() and used.exists() and total.exists()):
            continue
        try:
            entry = {
                "vendor": "amd",
                "name": _amd_card_name(dev),
                "util_pct": int(busy.read_text().strip()),
                "vram_used_mb": int(used.read_text().strip()) // (1024 * 1024),
                "vram_total_mb": int(total.read_text().strip()) // (1024 * 1024),
                "iGPU": _is_igpu(dev),
            }
            gtt_used = dev / "mem_info_gtt_used"
            gtt_total = dev / "mem_info_gtt_total"
            if gtt_used.exists() and gtt_total.exists():
                entry["gtt_used_mb"] = int(gtt_used.read_text().strip()) // (1024 * 1024)
                entry["gtt_total_mb"] = int(gtt_total.read_text().strip()) // (1024 * 1024)
            cards.append(entry)
        except (OSError, ValueError):
            pass
    # NVIDIA via nvidia-smi.
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=True, timeout=3,
            ).stdout.strip()
            for line in out.splitlines():
                parts = [s.strip() for s in line.split(",")]
                if len(parts) == 4:
                    cards.append({
                        "vendor": "nvidia",
                        "name": parts[0],
                        "util_pct": int(parts[1]),
                        "vram_used_mb": int(parts[2]),
                        "vram_total_mb": int(parts[3]),
                        "iGPU": False,
                    })
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
    return cards


def _amd_card_name(dev: Path) -> str:
    """Best-effort human name for an AMD card."""
    # /sys/class/drm/card1/device/product_name is sometimes present (newer kernels)
    pn = dev / "product_name"
    if pn.exists():
        try:
            n = pn.read_text().strip()
            if n:
                return n
        except OSError:
            pass
    # Fall back to lspci for the bus address of this device.
    uevent = dev / "uevent"
    if uevent.exists():
        try:
            for line in uevent.read_text().splitlines():
                if line.startswith("PCI_SLOT_NAME="):
                    slot = line.split("=", 1)[1].strip()
                    if shutil.which("lspci"):
                        out = subprocess.run(
                            ["lspci", "-mm", "-s", slot],
                            capture_output=True, text=True, timeout=2,
                        ).stdout.strip()
                        if out:
                            # lspci -mm fields are quoted; the third is the device name.
                            import shlex
                            fields = shlex.split(out)
                            if len(fields) >= 4:
                                return fields[3]
        except (OSError, subprocess.SubprocessError):
            pass
    return "amdgpu"


def _is_igpu(dev: Path) -> bool:
    """An iGPU has very small fixed VRAM (hundreds of MB) and large GTT, OR
    is on the CPU's PCI device path. Heuristic: VRAM <= 16 GB AND GTT >= VRAM."""
    try:
        v = int((dev / "mem_info_vram_total").read_text().strip()) // (1024 * 1024)
        gtt_path = dev / "mem_info_gtt_total"
        if gtt_path.exists():
            g = int(gtt_path.read_text().strip()) // (1024 * 1024)
            return v <= 16 * 1024 and g >= v
    except (OSError, ValueError):
        pass
    return False


# Tier classification. Order matters; the first match wins.
TIERS = [
    {
        "id": "tiny",
        "name": "Tiny (4-8 GB RAM, no dGPU)",
        "match": lambda h: h["ram"]["total_mb"] < 12_000 and not _has_real_gpu(h),
    },
    {
        "id": "laptop",
        "name": "Laptop (16-32 GB RAM, integrated GPU)",
        "match": lambda h: h["ram"]["total_mb"] < 48_000 and not _has_dgpu(h),
    },
    {
        "id": "halo",
        "name": "Big iGPU + unified RAM (Strix Point / Halo, Apple Silicon Pro/Max)",
        "match": lambda h: h["ram"]["total_mb"] >= 48_000 and any(g.get("iGPU") for g in h["gpus"]),
    },
    {
        "id": "workstation",
        "name": "Workstation (24+ GB dedicated GPU)",
        "match": lambda h: any(
            (not g.get("iGPU")) and g.get("vram_total_mb", 0) >= 22_000
            for g in h["gpus"]
        ),
    },
    {
        "id": "server",
        "name": "Server (multi-GPU or 64+ GB system RAM)",
        "match": lambda h: (
            len([g for g in h["gpus"] if not g.get("iGPU")]) >= 2
            or h["ram"]["total_mb"] >= 96_000
        ),
    },
    {
        "id": "mid",
        "name": "Mid consumer (fallback)",
        "match": lambda h: True,
    },
]


def _has_real_gpu(h):
    return len(h["gpus"]) > 0


def _has_dgpu(h):
    return any((not g.get("iGPU")) and g.get("vram_total_mb", 0) >= 4_000 for g in h["gpus"])


def detect_tier(snapshot=None):
    if snapshot is None:
        snapshot = system_snapshot()
    for tier in TIERS:
        if tier["match"](snapshot):
            return tier
    return TIERS[-1]


def system_snapshot():
    return {
        "cpu": cpu_info(),
        "ram": ram_info(),
        "gpus": gpu_info(),
    }


def fits_locally(model_entry, snapshot=None):
    """Returns ('yes' | 'spill' | 'no', explanation) for whether a catalog model fits."""
    if snapshot is None:
        snapshot = system_snapshot()
    needs_ram_gb = model_entry.get("needs_ram_gb", 0)
    fits_in_vram_gb = model_entry.get("fits_in_vram_gb", 0)
    total_ram_gb = snapshot["ram"]["total_mb"] / 1024
    best_vram_gb = max((g.get("vram_total_mb", 0) for g in snapshot["gpus"]), default=0) / 1024

    if needs_ram_gb and total_ram_gb < needs_ram_gb:
        return "no", f"needs {needs_ram_gb} GB RAM, have {total_ram_gb:.1f} GB"
    if fits_in_vram_gb and best_vram_gb >= fits_in_vram_gb:
        return "yes", f"fits in {best_vram_gb:.1f} GB VRAM"
    if any(g.get("iGPU") for g in snapshot["gpus"]):
        return "spill", "iGPU: weights mostly in system RAM, GPU compute helps"
    if total_ram_gb >= needs_ram_gb:
        return "spill", "CPU inference: will work, expect modest tokens/sec"
    return "no", "insufficient memory"
