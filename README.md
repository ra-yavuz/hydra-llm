# hydra-llm

**Docker for language models, with retrieval baked in.**

One CLI to download, run, chat with, and search local LLMs. Each model runs in its own Docker container over llama.cpp, so `docker ps` shows you exactly what is running and on which port. The CLI ships a hardware-aware curated catalog of community GGUFs that download anonymously (no Hugging Face token required), exposes OpenAI-compatible endpoints on stable local ports, and treats retrieval-augmented generation as a first-class feature: `hydra-llm index <path>`, `hydra-llm query "..."`, and `hydra-llm chat <model> --rag <path>`. With `hydra-llm create` you can bake a model, a persona, and a corpus into one alias and chat with it by name. Optional KDE Plasma 6 panel widget. No cloud, no API key, no telemetry.

> ## Disclaimer / no warranty
>
> This software runs large language models on your machine, manages Docker containers on your behalf, downloads multi-gigabyte model files from third-party hosts (primarily Hugging Face community mirrors), exposes HTTP APIs on local ports, and (when you use the RAG features) reads files inside any folder you index and stores embeddings of them on disk. It is provided **as is, without warranty of any kind**, express or implied, including but not limited to merchantability, fitness for a particular purpose, and noninfringement.
>
> By installing or running this software you accept that:
>
> - You alone are responsible for any damage to your hardware, data, network, or system.
> - The author(s) and contributors are **not liable** for any harm, data loss, hardware failure, security incident, model output, voided warranty, or other damages, however caused.
> - LLM weights and embedder weights downloaded via this tool are governed by their own upstream licenses (Llama, Gemma, Mistral, Qwen, Nomic, BGE, etc.). You are responsible for complying with each model's license. Some models prohibit specific uses; read the model card before using one in production.
> - LLM outputs are unreliable. They will hallucinate, repeat training data, give incorrect medical/legal/financial advice, and produce harmful or biased content. **Do not rely on them for safety-critical decisions.** RAG *reduces* hallucination but does not eliminate it; the model can still misquote retrieved chunks.
> - The RAG pipeline reads files in any directory you point `hydra-llm index` at and stores chunked text plus embeddings of those files at `<directory>/.hydra-index/`. If a directory contains secrets, credentials, or sensitive personal data, those will be embedded into a local LanceDB index. The index never leaves your machine, but anyone with read access to that directory can read the index. Audit what you index.
> - Running large models stresses CPU, RAM, and GPU. Sustained high utilisation can cause thermal throttling, fan wear, or in poorly-cooled systems, hardware damage. Monitor your machine.
> - The CLI shells out to `docker`. A misconfigured Docker setup or an attacker who can write to your config files could be used to run arbitrary containers as your user.
>
> If you do not accept these terms, do not install or run this software.
>
> Full legal license: see [`LICENSE`](LICENSE) (MIT).

## Why use this instead of plain Ollama or llama.cpp

Three answers, in increasing order of "you didn't know you wanted that":

1. **Transparent Docker over llama.cpp.** Each model runs in its own container with a stable name and a reserved port. `docker ps` shows you exactly what's running. No wrapper daemon you can't see into. `hydra-llm` is a thin layer over real, inspectable infrastructure.
2. **Hardware-aware curated catalog with anonymous downloads.** `hydra-llm list-online` filters community GGUFs to what your machine can actually run, scored against the tier from `hydra-llm doctor`. No Hugging Face account or token required. `HF_TOKEN` is honored but never demanded.
3. **RAG built in.** Index any folder with one command. Query it with another. Or add `--rag <path>` to `hydra-llm chat` and your model retrieves relevant chunks at every turn. Bundle a model + a persona + a corpus into one declarative alias and just say `hydra-llm chat my-bot`. Nobody else does that.

If you've used Ollama, this will feel familiar. The differences: no modelfiles to write, runs in plain Docker (no opaque background daemon), exposes the same OpenAI endpoint shape, and ships a real KDE panel widget. And it has retrieval. Ollama doesn't.

## Why Docker

Every model server (chat or embedder) runs in its own Docker container. The reasons:

- **No host pollution.** The llama.cpp engine, its CUDA/Vulkan runtime, and every model live inside a container. Nothing gets installed on your host beyond the `hydra-llm` CLI itself and a Python or two.
- **Trivial removal.** `sudo apt remove hydra-llm` uninstalls the CLI. `hydra-llm wipe` additionally tears down the engine image, every cached model, every embedder, and every chat session. Your host is back to where it started.
- **Reproducibility.** The same image runs the same way on a Strix iGPU laptop, a workstation with a discrete GPU, a NUC, or an Apple Silicon Mac via Docker Desktop. Two engine variants ship: Vulkan for GPU-equipped boxes and a plain CPU build for everything else; the right one is auto-selected per machine.
- **Inspectable.** `docker ps` tells you exactly which models are running and on which port. `docker logs hydra-<alias>` gives you the raw llama-server output. No background daemon hiding state behind an opaque API.
- **Per-model isolation.** Two models running side by side cannot interfere with each other's address space or file handles; they are separate processes in separate containers. Killing one never destabilises another.

## Where it runs

hydra-llm targets Linux first (Debian, Ubuntu, Fedora, Arch, etc.) but the architecture is portable wherever Docker runs:

- **Linux:** primary target. `.deb` for Debian/Ubuntu derivatives; the bash one-liner installer works on every distro with bash + Docker.
- **WSL2 on Windows:** works. Run hydra-llm inside an Ubuntu WSL distro with Docker Desktop integrated, or with the native `docker.io` package inside the WSL distro itself. The Vulkan engine uses WSLg's GPU passthrough on supported hardware; otherwise the CPU engine is used. The KDE Plasma widget needs Plasma 6, so it does not apply on Windows desktops; the CLI works fully.
- **macOS:** CLI runs under Docker Desktop. The Vulkan engine variant does not apply on Apple Silicon (use the CPU engine, which still benefits from Apple's accelerators inside Docker's HVF layer); model speeds are competitive on M-series Pro/Max chips.
- **Headless servers:** the CLI is the only surface needed. The Plasma widget and other GUI hooks degrade gracefully; nothing blocks on a missing display.

## What you actually get

- **Stable OpenAI-compatible endpoints.** Every running model exposes `POST /v1/chat/completions` on its own local port from your `port_range`. Point Aider, Continue.dev, Open Interpreter, [lillycoder](https://ra-yavuz.github.io/lillycoder/), or your own scripts at `http://localhost:18080/v1` and rotate which model is behind that port with `hydra-llm stop A && start B`. No client config changes, no API keys.
- **Container lifecycle without docker-fu.** `start`, `stop`, `stop-all`, `status`, `api`. Two engine images (Vulkan, CPU) are built locally on first `setup` and auto-selected from your hardware, with a CPU fallback if Vulkan misbehaves. Each model gets a stable container name (`hydra-<id>`) and a reserved port.
- **Hardware-aware curated catalog.** `hydra-llm list-online` filters community-quantized GGUFs (Bartowski, lmstudio-community, mradermacher) to what your machine can actually run. Tiers cover 4 GB Pi-class up to 70B-on-iGPU Strix-class boxes. All downloads work without a Hugging Face account.
- **RAG over any folder.** `hydra-llm index <path>` walks + classifies code-vs-prose + line-aware chunks + embeds each chunk + stores in `<path>/.hydra-index/` (LanceDB). `hydra-llm query "..."` uses Reciprocal Rank Fusion across the code and prose indexes. `hydra-llm chat <model> --rag <path>` augments every turn with retrieval. Federated query across all your indexed folders. Tags. Incremental refresh.
- **Catalog-bound bundles** (the headline). A chat-catalog entry can carry `system_prompt`, `params`, **and** a `rag_index:` path. `create <model> <persona.md> <id> --rag-index <path>` bakes all three into one alias. Then `chat <alias>` runs everything with no flags.
- **Optional KDE Plasma 6 panel widget.** Per-row Start/Stop, Console launcher, inline log pane, prompt/params editor, and a HAL-eye tray indicator that breathes with system load.
- **Personas, prompts, persistent sessions.** Reusable persona files, per-alias system prompts and sampling params (narrowest layer wins), and chat sessions saved as JSON you can resume.

## Quickstart

End to end. Zero to chatting with retrieval over your own folder. Every command below is real, in the order you run them. Deep-dive sections later in the README explain each piece.

### 1. Install (Debian / Ubuntu)

One line. Sets up the signed apt repo, refreshes the package index, installs the CLI. Idempotent:

```sh
sudo bash -c 'set -e; install -m 0755 -d /etc/apt/keyrings && curl -fsSL https://ra-yavuz.github.io/apt/pubkey.gpg -o /etc/apt/keyrings/ra-yavuz.gpg && echo "deb [signed-by=/etc/apt/keyrings/ra-yavuz.gpg] https://ra-yavuz.github.io/apt stable main" > /etc/apt/sources.list.d/ra-yavuz.list && apt update && apt install -y hydra-llm'
```

On KDE, optional panel widget:

```sh
sudo apt install hydra-llm-plasma
```

Already have the `ra-yavuz` apt repo? Then it is just `sudo apt update && sudo apt install hydra-llm`. Without `apt update`, apt will not see new versions.

<details><summary>Step by step (manual repo setup) and other distros</summary>

```sh
# Manual deb install (any apt-based distro):
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://ra-yavuz.github.io/apt/pubkey.gpg \
  | sudo tee /etc/apt/keyrings/ra-yavuz.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/ra-yavuz.gpg] https://ra-yavuz.github.io/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/ra-yavuz.list
sudo apt update
sudo apt install hydra-llm

# One-liner installer (any distro with bash + Docker; lands in ~/.local/bin):
curl -fsSL https://raw.githubusercontent.com/ra-yavuz/hydra-llm/main/get.sh | bash
```

</details>

If `docker ps` errors with permission denied:

```sh
sudo apt install docker.io
sudo usermod -aG docker "$USER"   # log out and back in
```

### 2. First-run engine setup

Build the llama.cpp Docker image and pull a starter model so you can confirm the install works:

```sh
hydra-llm doctor          # confirm hardware detection (tier, RAM, GPU)
hydra-llm setup           # build engine image (5-10 min) + starter model + smoke test
```

`hydra-llm setup` is the only step that takes real time. It builds the Vulkan and CPU engine images locally (the deb does not ship binaries; this is what the Docker dependency is for). On a typical laptop this is a one-time 5-10 minute investment.

### 3. Pick and download a real chat model

```sh
hydra-llm list-online                 # browse the catalog, filtered to what fits
hydra-llm download gemma-2-2b         # or any id from the list-online output
```

Pick something your hardware actually fits. `hydra-llm doctor` already told you the tier. Models tagged for your tier work; everything else either is too big or too small.

Want to register your own GGUFs from somewhere else (Ollama, LM Studio, manual download)? One command:

```sh
hydra-llm addlocal /path/to/your/gguf/folder/ --link --tier laptop
```

That recursively registers every `.gguf` under the folder.

### 4. Chat with the model

The simplest case: just type `hydra-llm chat`. With v0.2.3+, no alias is needed:

```sh
hydra-llm chat
# - exactly one model is running -> attaches to it
# - several running              -> lists with port numbers, prompt for a number
# - none running but one is downloaded -> auto-starts that one
# - none running, several installed    -> lists, prompt for a number
# - nothing installed             -> tells you to download one
```

You can always be explicit:

```sh
hydra-llm chat gemma-2-2b
hydra-llm chat 4                       # numeric index from `hydra-llm list`
```

Inside the REPL, slash commands matter: `/help` lists everything, `/quit` exits, `/reset` clears history, `/params` shows sampling params, `/set temperature 0.5` adjusts one mid-session.

Sessions persist by default. Each `(folder, model)` pair gets its own session at `~/.local/state/hydra-llm/sessions/<hash>-<alias>.json`, so chats in different folders never share history. Override with `--session foo` for a named cross-folder session, or `./chat.json` (positional) to pin a session file to your project.

### 5. Set up RAG (one-time)

Now the fun part: index a folder so the model can answer questions about *your* code or notes.

```sh
hydra-llm rag setup
```

This shows a numbered menu like:

```
Recommended embedder for your tier: qwen3-embed-4b  (2.5 GB)  (NOT installed)

Options:
  1. Download recommended (qwen3-embed-4b, 2.5 GB)
  2. Use already-installed nomic-embed-text (prose)
  3. Pick a different embedder from the catalog
  4. Cancel; do nothing
```

Pick `1` for the recommendation, or `2` if you already have a smaller embedder you'd rather use (especially if your folder is mostly prose: a novel, notes, docs). The "What is an embedder" section below explains the choice in more detail.

### 6. Index a folder

```sh
cd ~/path/to/whatever/folder/you/want/searched
hydra-llm index .
```

That walks the folder, classifies each file as code or prose, chunks it, embeds every chunk, and stores everything in `<folder>/.hydra-index/` (a LanceDB database, ~20-200 MB depending on folder size). Re-running `hydra-llm index .` is idempotent: it diffs `(mtime, size)` against the previous index and only re-embeds changed files, so it is fast on subsequent runs (~1 second for a no-op refresh).

Useful flags: `--exclude '*.test.js'`, `--include 'fixtures/important.json'`, `--depth 2`, `--max-file-size-mb 0.5`, `--full` (force from-scratch rebuild), `--dry-run` (print plan, do not embed), `--tag work` (label this store for federated `--tag work` queries from elsewhere later).

### 7. Sanity-check retrieval (no model needed)

Before wiring up a chat, make sure retrieval finds what you expect:

```sh
hydra-llm query "where do we handle auth tokens"
```

You get the top-K chunks with file path and line range. No chat model is involved, so this is a fast and cheap test. If the results look off, your index is the problem (try a different embedder, or `index . --full`); if they look right, you are good to chat.

### 8. Chat with retrieval

```sh
hydra-llm chat --rag .                          # use the index in cwd
hydra-llm chat gemma-2-2b --rag . --rag-show-chunks   # also echo retrieved locations each turn
```

Per turn, hydra embeds your message, fetches the top-K chunks from the index, and prepends them to the prompt as a `<context>...</context>` block before sending to the model. The `--rag-show-chunks` flag prints the chunk file paths so you can see what the model is being given.

Slash commands inside the REPL: `/rag on|off` toggles retrieval, `/rag-show on|off` toggles the `[rag: N chunks]` summary line, `/rag-chunks on|off` toggles the chunk preview, `/rag <text>` runs a one-off retrieval without a model call.

### 9. Bundle a model + persona + corpus into one alias (optional, headline feature)

If you find yourself doing the same `chat <model> --rag <path>` repeatedly for the same project:

```sh
hydra-llm create gemma-2-2b ~/personas/code-helper.md cool-app-bot \
    --rag-index ~/projects/cool-app
```

That bakes the model, the persona, and the corpus path into one declarative catalog entry called `cool-app-bot`. From now on:

```sh
hydra-llm chat cool-app-bot
```

No flags. Retrieval just works. Move the `~/.config/hydra-llm/catalog.yaml` file across machines and the bundle moves with it.

### 10. Federate: query across every indexed folder

If you have indexed several folders, you can search them all at once from anywhere:

```sh
hydra-llm rag stores                           # list every folder you have indexed
hydra-llm query "..." --all                    # federated search across all of them
hydra-llm query "..." --tag work               # only stores tagged with 'work'
hydra-llm chat <model> --rag-all               # chat with retrieval across all stores
```

That is the whole arc, end to end. Everything below is depth on each piece.

## Personalities, prompts, and params

Three layers, narrowest wins:

| Layer | Where it lives | Scope |
|---|---|---|
| Persona | `~/.config/hydra-llm/personas/<name>.md` (markdown + optional YAML front matter) | Reusable across models. Selected with `--persona <name>`. |
| Per-alias system prompt | `~/.config/hydra-llm/prompts/<alias>.txt` | Always applied to that catalog id, unless a persona is given. |
| Per-alias sampling params | `~/.config/hydra-llm/params/<alias>.json` | Same idea, for `temperature`, `top_p`, `top_k`, `repeat_penalty`, `max_tokens`, `seed`. |

Inside the chat REPL: `/params` shows the currently active values, `/set <key> <value>` changes one for the session only, `/reset` clears history but keeps the system prompt, `/thoughts on|off` toggles reasoning output for models that emit it.

### Bake a persona into a permanent alias

If you keep typing `--persona <name>` for the same combination, freeze it as its own catalog entry:

```sh
hydra-llm create gemma-2-2b ~/personas/coolguy.md
# -> registers alias 'gemma-2-2b-coolguy', sharing gemma-2-2b's GGUF (no extra download)

hydra-llm chat gemma-2-2b-coolguy
# system prompt and any front-matter temperature/max_tokens applied automatically

hydra-llm create gemma-2-2b ~/personas/coolguy.md mymodel
# -> custom alias name 'mymodel' instead of the auto-derived one
```

The persona file is read once and inlined into the new entry's `system_prompt` field, so the alias keeps working even if you later move or delete the original `.md` file. The new alias inherits the base model's port reservation; pass `--port` to override.

### Pin a chat session to a path

The default `hydra-llm chat <alias>` uses a centralized session at `~/.local/state/hydra-llm/sessions/default.json`, resumable via `-s/--session <name>`. Pass a positional path instead to keep the session next to whatever it's about:

```sh
hydra-llm chat gemma-2-2b ./project-notes.json
# resumes ./project-notes.json if it exists, creates it otherwise
```

## RAG: index a folder, chat with retrieval

RAG (retrieval-augmented generation) means: when you ask a question, hydra retrieves relevant chunks from a corpus and prepends them to the prompt, so the model answers based on text it didn't have to memorise.

### What is an embedder

An **embedder** is a small specialised model that does one thing: take a piece of text in, output a fixed-size list of numbers (a vector) out. Different texts that mean similar things produce vectors that are mathematically close. That is what makes "find the chunk most similar to this question" possible: the question becomes a vector, every chunk in your folder is already a vector, and finding nearest neighbours is just arithmetic.

Embedders are not chat models. They do not generate text. They run in their own `llama-server --embeddings` containers (separate port range from chat models, default 19080-19099) and live in a separate catalog at `~/.config/hydra-llm/embedders.yaml`. Six curated embedders ship: `nomic-embed-text` (lightweight, prose-leaning), `qwen3-embed-{0.6b,4b,8b}` (instruction-aware, strong on code), `bge-m3` (multilingual), `nomic-embed-code` (code-tuned).

### Single-embedder vs dual-index mode

By default, hydra runs **single-embedder mode**: one embedder serves all your chunks (both code and prose). One GGUF on disk, one container at query time, one LanceDB index per folder. Cheap, simple, fine for most personal-scale projects.

There is also an opt-in **dual-index mode** that runs *two* embedders (one tuned for code, one for prose), maintains a separate index per kind, and fuses query results via Reciprocal Rank Fusion at retrieval time. The theory: code questions surface from the code table, prose questions surface from the prose table, fusion gives you the best of both.

Most users should stay on single. The honest numbers: dual-index buys measurable retrieval quality on very large mixed corpora (think a monorepo with thousands of source files plus hundreds of design docs), but for personal-scale folders the marginal quality difference is small. The cost is two embedder downloads (often 4+ GB combined), two embedder containers running concurrently, two indexes per folder, and the fusion logic at query time.

Turn dual mode on later with:

```sh
hydra-llm rag setup --dual                # one-off
# or persist:
echo 'rag: { dual_index: true }' >> ~/.config/hydra-llm/config.yaml
```

Switching modes requires a one-time re-index of any existing folders.

### 1. First-run for RAG

```sh
hydra-llm rag setup
```

Detects your hardware tier and presents a numbered menu:

```
Recommended embedder for your tier: qwen3-embed-4b  (2.5 GB)  (NOT installed)
  Default code embedder for halo+ tier. Instruction-aware ...

Options:
  1. Download recommended (qwen3-embed-4b, 2.5 GB)
  2. Use already-installed nomic-embed-text (prose)
  3. Pick a different embedder from the catalog
  4. Cancel; do nothing

Number [1-4, default 1]:
```

If you already have an embedder on disk (from a previous test, or from a manual `hydra-llm rag download`), it shows up as option 2 so you don't get pushed into a 2.5 GB download you don't need.

Browse and pick manually any time:

```sh
hydra-llm rag list-online            # catalog, filtered to hardware
hydra-llm rag download <id>          # pull one
hydra-llm rag info <id>              # dimensions, pooling, prefix, running state
hydra-llm rag list                   # what is installed locally
```

### 2. Index any folder

```sh
cd ~/projects/cool-app
hydra-llm index .                    # full index on first run
hydra-llm index .                    # incremental: diffs by (mtime, size)
hydra-llm index . --tag work         # tag this store (--tag is repeatable)
hydra-llm index . --exclude '*.test.js' --include 'fixtures/important.json'
hydra-llm index . --depth 2 --max-file-size-mb 0.5
hydra-llm index . --full             # force from-scratch rebuild
hydra-llm index . --dry-run          # print plan, don't embed
```

The walker uses `.gitignore` (via `python-pathspec`) plus a builtin blacklist (`node_modules`, `.venv`, `target`, `build`, lockfiles, binaries, archives, media, weights, files >1 MB). Each file is classified *code* or *prose* by extension first (`.py`, `.sh`, `.md`, `.rst`, ...), then by canonical basenames (`Makefile`, `Dockerfile`, `README`, `LICENSE`), then by shebang sniff. Code goes to a code embedder; prose to a prose embedder. The chunker is line-aware (1500 chars target, 200 overlap, never splits mid-line).

Each indexed folder grows a `.hydra-index/` with two LanceDB tables (`code.lance`, `prose.lance`), a `meta.yaml` recording which embedders the index was built with, and a `files.json` that drives incremental refresh. The `.hydra-index/` moves with the folder: copy a project to another machine, the index comes along.

### 3. Query the index

```sh
hydra-llm query "where do we handle auth tokens?" --in .
hydra-llm query "..." --top-k 10 --code-only
hydra-llm query "..." --tag work          # federated across all tagged stores
hydra-llm query "..." --all               # federated across every indexed folder
```

Default scope: if cwd has a `.hydra-index/`, query just that store. Otherwise, query every registered store (federated). Override with `--in`, `--stores`, or `--tag`.

Behind the scenes: the question is embedded with *both* embedders, top-K hits come back from each table, and the lists are fused by Reciprocal Rank Fusion (`k=60`). This is the 2026 best practice for code+prose corpora; it avoids the failure mode where a code embedder mangles README prose, while still surfacing the right code blocks first.

### 4. Chat with retrieval

```sh
hydra-llm chat llama-3.1-8b --rag .              # single store
hydra-llm chat llama-3.1-8b --rag-all            # every registered store
hydra-llm chat llama-3.1-8b --rag-tag work       # tag-scoped federation
hydra-llm chat llama-3.1-8b --rag-top-k 5 --rag-show-chunks
```

Per-turn retrieval. Every user message is embedded, top-K chunks come back, and the message becomes:

```
<context>
--- src/auth/middleware.go:42-78 ---
func authenticate(r *http.Request) ...
--- README.md:54-72 ---
Auth flow: ...
</context>

(your original question)
```

Saved sessions keep the original (un-augmented) text so resumes don't carry stale context. New REPL slash commands: `/rag on|off`, `/rag-show on|off`, `/rag-chunks on|off`, `/rag <text>` for one-off retrieval without a model call.

### 5. Catalog-bound bundles (the headline feature)

A chat-catalog entry can carry a `rag_index:` field. `create` bakes that field plus a persona's body plus its front-matter params into a new alias.

```sh
hydra-llm create llama-3.1-8b ~/personas/senior-engineer.md cool-app-bot \
    --rag-index ~/projects/cool-app

# Persisted to ~/.config/hydra-llm/catalog.yaml as one declarative entry:
#   id: cool-app-bot
#   filename: Llama-3.1-8B-Instruct-Q4_K_M.gguf   (shared with the base, no extra download)
#   system_prompt: "You are a senior engineer ..."
#   rag_index: /home/yavuz/projects/cool-app
#   tags: [persona-baked, rag-bound]
#
# Now:
hydra-llm chat cool-app-bot                       # no flags. retrieval just works.
```

Other local-RAG CLIs let you index a folder and chat with retrieval. Nobody else treats *model + persona + corpus* as a single declarative unit you can refer to by name. Move the catalog file across machines, and the bundle moves with it.

## Plasma widget (the headline UI for KDE 6 users)

```sh
sudo apt install hydra-llm-plasma
```

Then right-click the panel -> **Add or Manage Widgets...** -> drag **Hydra LLM** onto the panel. The widget reads the same `~/.config/hydra-llm/` as the CLI, so anything you registered with `addlocal` shows up automatically.

Per-row controls:

- **Start / Stop**: brings the container up or tears it down. Starting a model auto-opens the inline log console below the model list, so you can watch the model load.
- **Console**: opens your terminal emulator running `hydra-llm chat <alias>` (tries `konsole` -> `gnome-terminal` -> `alacritty` -> `kitty` -> `xfce4-terminal` -> `xterm` -> `x-terminal-emulator`, first found wins).
- **Logs**: toggle the inline `docker logs --tail 80` pane for any running model.
- **Configure**: opens an editor for the system prompt and sampling params for that alias. Inline values from your catalog take precedence over the file editor; the editor disables Save in that case.

The **HAL-eye indicator** in the panel breathes faster as CPU/RAM/GPU/VRAM utilisation goes up, shows a yellow scanning ring while a container is loading, and turns solid red once at least one model is healthy.

Native widgets for GNOME, XFCE, and others are on the roadmap. On every other desktop the CLI works fully today; see [INTEGRATIONS.md](INTEGRATIONS.md).

## Pairs with lillycoder

[`lillycoder`](https://github.com/ra-yavuz/lillycoder) is a sibling project: a local-first coder REPL with file and shell tools that talks to any OpenAI-compatible `/v1` endpoint. hydra-llm exposes exactly that endpoint, so the two compose into a fully local coding agent in one terminal:

```sh
# in hydra-llm: pick something good at code
hydra-llm start qwen2.5-32b
hydra-llm api   qwen2.5-32b           # prints the URL

# in your project directory:
lillycoder --api http://localhost:18087/v1
# (lilly auto-detects common local LLM ports too, so just `lillycoder` often works)
```

hydra-llm manages the model server (download, start, stop, watch, system prompts). lillycoder is the agent that sits in front of it: reads/writes files, runs shell commands, greps your codebase, all under a permission gate. No cloud, no API key, no telemetry on either end.

## API access (for any OpenAI-compatible client)

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
| `halo`       | 48+ GB unified RAM, big iGPU (Strix Point/Halo, Apple Silicon Pro/Max) | Gemma-3-27B Q4, Qwen-2.5-32B Q4 |
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
