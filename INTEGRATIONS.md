# Desktop integrations roadmap

`hydra-llm` is a CLI first; every feature works in any terminal on any Linux distro. The native UI integrations below are *nice-to-haves* that surface the same functionality on each desktop's panel.

## Status

| Desktop / WM        | Native UI status      | What works today                                         |
|---                  |---                    |---                                                       |
| KDE Plasma 6        | Shipped (`hydra-llm-plasma`) | HAL-eye panel applet with start/stop/chat/logs/edit |
| GNOME Shell         | Planned               | CLI, plus generic SNI tray (see below)                   |
| XFCE                | Planned               | CLI, plus generic SNI tray                               |
| Cinnamon            | Planned               | CLI, plus generic SNI tray                               |
| MATE                | Planned               | CLI, plus generic SNI tray                               |
| LXQt                | Planned               | CLI                                                      |
| i3 / Sway / Hyprland| Planned               | CLI                                                      |

## Auto-detection

`hydra-llm doctor` prints the detected desktop and tells you which (if any) native widget package fits. `hydra-llm setup` does the same at the end of first-run setup.

The `get.sh` per-user installer copies the Plasmoid into `~/.local/share/plasma/plasmoids/` when KDE Plasma is detected. On other desktops it installs only the CLI. No surprise installs on headless boxes.

## Generic SNI tray (planned)

A standalone `hydra-llm-tray` Python app that talks to the same `hydra-llm tray status` JSON API as the Plasmoid. Will provide an `org.freedesktop.StatusNotifierItem` so it shows up in any tray that supports SNI:

- GNOME (with the AppIndicator extension shipped on most distros)
- XFCE, MATE, Cinnamon, LXQt
- waybar, i3bar, polybar (if configured for SNI)
- Hyprland with `hyprland-bar` or `waybar`

Right-click menu mirrors the Plasmoid popup: per-model start/stop/chat/logs, system-prompt and sampling-params editor, GPU and CPU snapshot.

## GNOME Shell extension (planned)

A dedicated extension distributed via [extensions.gnome.org](https://extensions.gnome.org). Same conceptual UI as the Plasmoid (HAL-eye topbar icon, popup with model rows, log console). Maintained per major GNOME version because of the extension API churn.

## Per-distro packaging roadmap

| Distro family                    | Today          | Planned                |
|---                               |---             |---                     |
| Debian, Ubuntu, Mint, Pop!_OS    | `.deb` shipped | apt source on Pages    |
| Fedora, RHEL, Rocky, openSUSE    | source tree    | `.rpm` via CI          |
| Arch, Manjaro, EndeavourOS       | source tree    | AUR `PKGBUILD`         |
| Alpine                           | source tree    | `.apk`                 |
| NixOS                            | source tree    | `default.nix` flake    |
| Gentoo                           | source tree    | ebuild                 |

The CLI itself is just `python3` + `bash` + `docker`, so the source tree runs anywhere with those three.

## Contributions

If you maintain one of the planned integrations and want to upstream a port, open an issue first so we can agree on the JSON contract (`hydra-llm tray status`, `hydra-llm tray logs`, `hydra-llm tray chat-spawn`, plus the prompt + params editor subcommands). The Plasmoid in `plasmoid/contents/ui/main.qml` is the canonical reference for what a fully-featured integration looks like.
