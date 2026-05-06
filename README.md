# hydra-llm

**Run local LLMs the easy way. Pick a model, download it, chat with it. No cloud, no API key, no telemetry.**

> ## Disclaimer / no warranty
>
> This software runs large language models on your machine, manages Docker containers on your behalf, downloads multi-gigabyte model files from third-party hosts (primarily Hugging Face community mirrors), and exposes HTTP APIs on local ports. It is provided **as is, without warranty of any kind**, express or implied, including but not limited to merchantability, fitness for a particular purpose, and noninfringement.
>
> By installing or running this software you accept that:
>
> - You alone are responsible for any damage to your hardware, data, network, or system.
> - The author(s) and contributors are **not liable** for any harm, data loss, hardware failure, security incident, model output, voided warranty, or other damages, however caused.
> - LLM weights downloaded via this tool are governed by their own upstream licenses (Llama, Gemma, Mistral, Qwen, etc.). You are responsible for complying with each model's license. Some models prohibit specific uses; read the model card before using one in production.
> - LLM outputs are unreliable. They will hallucinate, repeat training data, give incorrect medical/legal/financial advice, and produce harmful or biased content. **Do not rely on them for safety-critical decisions.**
> - Running large models stresses CPU, RAM, and GPU. Sustained high utilisation can cause thermal throttling, fan wear, or in poorly-cooled systems, hardware damage. Monitor your machine.
> - The CLI shells out to `docker`. A misconfigured Docker setup or an attacker who can write to your config files could be used to run arbitrary containers as your user.
>
> If you do not accept these terms, do not install or run this software.
>
> Full legal license: see [`LICENSE`](LICENSE) (MIT).

## What it is

A command-line tool plus optional KDE Plasma widget for managing local language models. It wraps [llama.cpp](https://github.com/ggerganov/llama.cpp) in Docker, gives you a curated catalog of GGUF models that download anonymously, and provides a single-binary CLI to start, stop, chat, and monitor them.

## Why

Existing options are either too magic (you don't know what's running) or too raw (manual Docker, manual ports, manual config). hydra-llm sits in the middle: it's transparent (one config file, real Docker containers you can see), but ergonomic (one command to download a model, one to chat).

## Quick start

### 1. Add the apt repo (one time)

```sh
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://ra-yavuz.github.io/apt/pubkey.gpg \
  | sudo tee /etc/apt/keyrings/ra-yavuz.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/ra-yavuz.gpg] https://ra-yavuz.github.io/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/ra-yavuz.list
sudo apt update
```

### 2. Install

```sh
sudo apt install hydra-llm                  # add `hydra-llm-plasma` if on KDE
```

hydra-llm runs every model in a Docker container. If `docker ps` errors with permission denied:

```sh
sudo apt install docker.io
sudo usermod -aG docker "$USER"   # log out / back in for the group to take effect
```

### 3. First-run setup

```sh
hydra-llm doctor          # detect your hardware tier
hydra-llm setup           # build engine image (5-10 min) + tiny starter model + smoke test
```

### 4. Pick and run a model

```sh
hydra-llm list-online            # filtered to what your hardware can run
hydra-llm download gemma-2-2b
hydra-llm chat gemma-2-2b        # auto-starts the container, drops you into a REPL
```

Or with a personality:

```sh
# Drop a markdown file in ~/.config/hydra-llm/personas/
hydra-llm chat gemma-2-2b --persona friendly-tutor
```

## Personalities, prompts, and params

Three layers, narrowest wins:

| Layer | Where it lives | Scope |
|---|---|---|
| Persona | `~/.config/hydra-llm/personas/<name>.md` (markdown + optional YAML front matter) | Reusable across models. Selected with `--persona <name>`. |
| Per-alias system prompt | `~/.config/hydra-llm/prompts/<alias>.txt` | Always applied to that catalog id, unless a persona is given. |
| Per-alias sampling params | `~/.config/hydra-llm/params/<alias>.json` | Same idea, for `temperature`, `top_p`, `top_k`, `repeat_penalty`, `max_tokens`, `seed`. |

Inside the chat REPL: `/params` shows the currently active values, `/set <key> <value>` changes one for the session only, `/reset` clears history but keeps the system prompt, `/thoughts on|off` toggles reasoning output for models that emit it.

## Plasma widget

If you installed `hydra-llm-plasma`, drop the **Hydra LLM** widget on your panel. Each row has buttons for:

- **Start / Stop**: brings the container up or tears it down. Starting a model auto-opens the inline log console below the model list, so you can watch the model load.
- **Console**: opens your terminal emulator running `hydra-llm chat <alias>` (tries `konsole` → `gnome-terminal` → `alacritty` → `kitty` → `xfce4-terminal` → `xterm` → `x-terminal-emulator`, first found wins).
- **Logs**: toggle the inline `docker logs --tail 80` pane for any running model.
- **Configure**: opens an editor for the system prompt and sampling params for that alias. Inline values from your catalog take precedence over the file editor; the editor disables Save in that case.

The HAL-eye indicator in the panel breathes faster as CPU/RAM/GPU/VRAM utilisation goes up, shows a yellow scanning ring while a container is loading, and turns solid red once at least one model is healthy.

Or as an HTTP API:

```sh
hydra-llm start gemma-2-2b
hydra-llm api gemma-2-2b
# → curl -s http://localhost:18080/v1/chat/completions ...
```

## Hardware tiers

| Tier         | Spec example                         | Recommended models |
|---           |---                                   |---|
| `tiny`       | 4-8 GB RAM, no dGPU                  | TinyLlama, Phi-3-mini, Gemma-2-2B |
| `laptop`     | 16-32 GB RAM, integrated GPU         | Llama-3.1-8B Q4, Mistral-7B Q4 |
| `halo`       | 64-128 GB unified RAM, AMD Strix iGPU| Gemma-3-27B Q4, Qwen-2.5-32B Q4 |
| `workstation`| 24+ GB dGPU                          | Llama-3.3-70B Q4 |
| `server`     | multi-GPU or 64+ GB system           | Mixtral-8x22B Q4, Llama-3.3-70B Q5 |

`hydra-llm doctor` detects your hardware and recommends a tier automatically.

## Anonymous downloads

The shipped catalog only references community-quantized GGUFs (Bartowski, lmstudio-community, mradermacher). All download anonymously, no Hugging Face account or token required. If you want gated models (official `meta-llama/*` or `google/gemma-*` repos), set `HF_TOKEN` in your environment; the CLI passes it through. The CLI never prompts for, stores, or transmits credentials beyond the download.

## Bring your own models

Already have a folder of GGUFs from Ollama, LM Studio, or a manual download? Two steps:

### 1. Point hydra-llm at the folder (optional)

```sh
mkdir -p ~/.config/hydra-llm
cat > ~/.config/hydra-llm/config.yaml <<'YAML'
models_dir: /path/to/your/existing/gguf/folder
YAML
```

The CLI and the Plasma widget both read `models_dir` from this single config, so the widget will see your models too. If you skip this step, hydra-llm uses `~/.local/share/hydra-llm/models/` by default and you can still register files that live elsewhere via `--link` (below).

### 2. Register each file

```sh
# File already lives under models_dir:
hydra-llm addlocal /path/to/Llama-3.1-8B-Instruct-Q4_K_M.gguf \
  --tier laptop --vram-gb 6 --ram-gb 12

# File lives somewhere else; create a symlink under models_dir:
hydra-llm addlocal ~/some/where/My-Finetune-Q4_K_M.gguf \
  --link --tier workstation --vram-gb 24
```

`addlocal` derives a sensible id from the filename, picks the next free port from your `port_range`, fills in the file size automatically, and writes a YAML entry to `~/.config/hydra-llm/catalog.yaml`. Useful flags:

| Flag | Effect |
|---|---|
| `--id <slug>` | override the auto-derived id |
| `--name "..."` | human-readable name shown in `list` |
| `--tier <t>` | repeat for multiple: `tiny`, `laptop`, `halo`, `workstation`, `server` |
| `--ram-gb`, `--vram-gb` | feeds the `FIT` column in `hydra-llm list` |
| `--gpu-layers` | layers to offload (default 99 = all) |
| `--port` | override the auto-picked port |
| `--family`, `--license`, `--context` | optional metadata |
| `--link` | symlink the file into `models_dir` if it lives elsewhere |
| `--replace` | overwrite an existing entry with the same id |
| `--yes` | skip the confirmation prompt |

You can always edit `~/.config/hydra-llm/catalog.yaml` by hand afterwards. User entries override shipped entries with the same id.

Where things live, in case you need to reach in by hand:

| What | Default path |
|---|---|
| Downloaded models | `~/.local/share/hydra-llm/models/` (override with `models_dir`) |
| User config + personas + prompts + params | `~/.config/hydra-llm/` |
| Chat sessions | `~/.local/state/hydra-llm/sessions/` |
| Cache | `~/.cache/hydra-llm/` |

## Privacy

- No telemetry, no analytics, no auto-update calls.
- All inference runs locally in Docker on your machine.
- Chat sessions are saved to `~/.local/state/hydra-llm/sessions/` as JSON. Delete them whenever you want; the CLI never uploads them.

## Components

| Component | Purpose | Optional |
|---|---|---|
| `hydra-llm` | core CLI | required |
| `hydra-llm-plasma` | KDE Plasma 6 panel widget | optional |

The installer detects your desktop and offers the right native UI when one exists. On Plasma 6, the widget is installed automatically. On every other desktop the CLI works fully today; native UIs for GNOME, XFCE, and others are on the roadmap. See [INTEGRATIONS.md](INTEGRATIONS.md).

## Uninstall

```sh
# .deb install:
sudo apt remove hydra-llm hydra-llm-plasma   # add --purge to also drop config

# user-mode install (the curl/bash one-liner):
hydra-llm uninstall                           # keeps configs and downloaded models
hydra-llm wipe                                # also deletes models, sessions, engine image
```

Both paths take care of stopping running model containers and removing the Plasma widget files. If you had the widget on your KDE panel, the user-mode uninstaller restarts `plasmashell` for you so the tray icon clears immediately. After `apt remove hydra-llm-plasma`, log out and back in (or run `kquitapp6 plasmashell && kstart plasmashell`) to refresh the panel.

## Documentation

- [Project page](https://ra-yavuz.github.io/hydra-llm/)
- [Hub of all ra-yavuz/* projects](https://ra-yavuz.github.io)

## License

MIT. See [`LICENSE`](LICENSE).
