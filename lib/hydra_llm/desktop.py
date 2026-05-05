"""Detect the running desktop environment so we can offer the right native UI."""
import os
import shutil
import subprocess


# Map detected DE id to (display name, supported widget package or None, hint text).
# Order matters: first match in detect() wins.
_KNOWN_DES = [
    ("kde",       "KDE Plasma 6",          "hydra-llm-plasma",
     "Native panel widget available."),
    ("gnome",     "GNOME",                 None,
     "GNOME extension is on the roadmap. CLI works fully today."),
    ("xfce",      "XFCE",                  None,
     "XFCE applet is on the roadmap. CLI works fully today."),
    ("cinnamon",  "Cinnamon",              None,
     "Cinnamon applet is on the roadmap. CLI works fully today."),
    ("mate",      "MATE",                  None,
     "MATE panel applet is on the roadmap. CLI works fully today."),
    ("lxqt",      "LXQt",                  None,
     "LXQt applet is on the roadmap. CLI works fully today."),
    ("hyprland",  "Hyprland",              None,
     "Generic SNI tray icon is on the roadmap; works in waybar."),
    ("sway",      "Sway",                  None,
     "Generic SNI tray icon is on the roadmap; works in waybar."),
    ("i3",        "i3",                    None,
     "Generic SNI tray icon is on the roadmap; works in i3bar."),
]


def detect() -> dict:
    """Returns {id, name, widget_package, hint, headless}.

    'headless' is True when no graphical session is detected (CI, server,
    SSH-only). In that case widget_package is None.
    """
    env = (os.environ.get("XDG_CURRENT_DESKTOP") or
           os.environ.get("DESKTOP_SESSION") or "").lower()
    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")

    # Headless detection. If we have neither $DISPLAY nor a wayland socket and
    # no XDG_CURRENT_DESKTOP, treat as headless.
    if not display and not env:
        return {
            "id": "headless",
            "name": "Headless / no graphical session",
            "widget_package": None,
            "hint": "Running without a desktop. Use the CLI; no widget needed.",
            "headless": True,
        }

    for known_id, name, pkg, hint in _KNOWN_DES:
        if known_id in env:
            return {
                "id": known_id, "name": name, "widget_package": pkg,
                "hint": hint, "headless": False,
            }

    # Last-ditch: look for plasmashell process even if XDG_CURRENT_DESKTOP is unset.
    try:
        out = subprocess.run(["pgrep", "-x", "plasmashell"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return {
                "id": "kde", "name": "KDE Plasma 6 (detected via process)",
                "widget_package": "hydra-llm-plasma",
                "hint": "Native panel widget available.",
                "headless": False,
            }
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return {
        "id": "unknown",
        "name": f"Unknown desktop ({env or 'unset'})",
        "widget_package": None,
        "hint": "No tested widget for this desktop yet. CLI works fully.",
        "headless": False,
    }


def has_apt() -> bool:
    return shutil.which("apt-get") is not None


def is_widget_installed(widget_package: str) -> bool:
    """Best-effort check via dpkg, falls back to checking the plasma plugin path."""
    if not widget_package:
        return False
    if shutil.which("dpkg"):
        try:
            r = subprocess.run(["dpkg", "-s", widget_package],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and "Status: install ok installed" in r.stdout:
                return True
        except subprocess.SubprocessError:
            pass
    # Plasmoid-specific fallback: the file is there even on non-deb installs.
    if widget_package == "hydra-llm-plasma":
        from pathlib import Path
        for p in (
            Path("/usr/share/plasma/plasmoids/com.github.ra-yavuz.hydra-llm"),
            Path("/usr/local/share/plasma/plasmoids/com.github.ra-yavuz.hydra-llm"),
            Path.home() / ".local/share/plasma/plasmoids/com.github.ra-yavuz.hydra-llm",
        ):
            if p.is_dir():
                return True
    return False
