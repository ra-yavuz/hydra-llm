# hydra-llm roadmap

Things deferred from past releases or noticed during use that haven't been
shipped yet. Not promises, not deadlines: just a written list so the next
working session has somewhere to start. Items roughly ordered by how much
real friction they remove for the user.

## v0.2.6 candidates

### Content-aware embedder selection

Right now `hydra-llm rag setup` recommends one embedder per hardware tier
(qwen3-embed-4b on halo, etc.) with no awareness of what the user is going
to index. A user with a folder full of prose (a novel, notes, design docs)
gets pushed into a 2.5 GB code-tuned embedder that is 30x slower than
nomic-embed-text on prose, with no quality benefit on prose.

The fix: when `hydra-llm index .` runs, the walker already classifies every
file as code or prose before any embedding happens. Use that signal to
pick a better default embedder than the tier default:

  > 80% prose       -> nomic-embed-text  (small, prose-tuned)
  > 80% code        -> qwen3-embed-4b    (default code)
  mixed 40-80% code -> qwen3-embed-4b    (handles prose acceptably)

Surface in the planning output:

    Detected: 100% prose (56 files)
    Recommended embedder: nomic-embed-text (84 MB, prose-tuned)
    You currently have qwen3-embed-4b configured. Use the recommendation? [Y/n]

This works for the novel case, the code-heavy case, and the mixed case.
Concrete change: extend `_recommend_embedders` in cli.py to take a
`walk_summary` argument; have `plan_index` consult it before defaulting.

### Hint when dual mode would actually help

Related: when the walker detects a genuinely mixed corpus (40-60% code +
40-60% prose, big enough that retrieval quality matters), surface a one-
liner:

    This folder is mixed; if you want top retrieval quality on the prose
    half, re-run with --dual-index (adds nomic-embed-text, +84 MB).

So the user is told the option exists when it might matter; default
stays the cheap single-embedder path.

### `hydra-llm chat help` (and friends)

Every subcommand should accept a `help` subverb in addition to `--help`,
because that's the form a new user reaches for first. `hydra-llm chat
help`, `hydra-llm rag setup help`, etc. The standardised path: each
subparser registers a `help` action that prints its own help and exits.

## v0.3.0 candidates

### Embedder idle-TTL reaper

Each embedder sidecar stays running indefinitely after the first query.
On a multi-folder workflow with the qwen3-embed-4b embedder loaded, that
is ~3 GB of VRAM held forever. Manual workaround today: `hydra-llm rag
stop-all` or the per-command `--stop-embedder` flag.

The right fix: a small per-user reaper (could be a systemd user timer or
just a "last-touched" file the next CLI invocation reads) that stops
embedder sidecars after `embedder_idle_ttl_seconds` of no traffic. The
config key already exists with a 60-second default; only the enforcement
is missing.

### SHA verification on download

The corrupt-download episode that motivated the embedder auto-recover in
v0.2.5 would never have happened if the catalog had SHA256 entries the
downloader could verify against. The hooks are already there (see
`downloader.download(catalog_entry["sha256"])`); the catalog YAMLs just
need the field populated.

Concrete work: a script that walks `catalog/catalog.yaml` and
`catalog/embedders.yaml`, computes the SHA256 of the upstream resolved
URL by streaming a HEAD-then-GET, and writes it back. Run it once,
review the diff, commit. From then on, the catalog has truth and bad
downloads fail loudly instead of silently corrupting an index.

### `hydra-llm doctor` shows engine status

`doctor` currently reports hardware. It should also report the engine
image age and the llama.cpp ref it was built from (the LABEL we added
in v0.2.5), so a user troubleshooting a model-load failure can see
"oh, my image is from 2026-04-12 against ref abc123" at a glance.

### Embedder per project, recorded in catalog

Today the embedder choice is global (default_code_embedder /
default_prose_embedder in config.yaml). The user noted that different
projects might benefit from different embedders. The cleanest design:
record the chosen embedder per-folder in `meta.yaml` (already there),
and surface it on `hydra-llm rag stores`. Then "switching back to a
folder" automatically uses the embedder it was indexed with, even if
the global default has since changed.

This already works at retrieval time (meta.yaml is consulted). What is
missing is the workflow on first index in a new folder where hydra
should ask "what should this project use" rather than blindly
inheriting the global.

## Larger ideas, not yet committed to

### Plasma widget surfaces RAG state

The Plasma widget currently shows chat-model rows. It does not show
indexed folders, embedder running state, or RAG-bound bundles
(despite tray.py already exposing `rag_index` per row). A "RAG" tab
or section in the popup would let users:

- See which folders they have indexed
- One-click reindex
- See which embedder containers are running
- Stop them with one click

Kept out of scope this release because the tray-side data is already
exposed; this is purely a QML/UI add. Worth doing once the rest of the
RAG flow is settled.

### Chat-model auto-recover on corrupt GGUF

We added auto-recover for embedder sidecars (re-download once on
gguf parse failure) in v0.2.5. The same logic applies to chat-model
containers: if a downloaded chat-model GGUF is corrupt, hydra
currently bubbles up the docker-run failure to the user. Lifting the
embedder helper to handle chat models too is small (the `_ensure_embedder
_with_repair` wrapper is mostly model-species-agnostic). Skipped for
v0.2.5 because the immediate friction was on embedders.

### MCP server for direct integration with Claude Code et al

Most LLM coding agents (Claude Code, Aider, Continue.dev, ...) speak MCP
or some equivalent. hydra-llm exposes OpenAI-compatible endpoints
already, but there is no MCP surface that says "here is a `retrieve`
tool that searches my indexed corpora". Building one would let any MCP
client tap hydra-llm's RAG without the user having to wrap the CLI.
Note: this overlaps with the standalone `claude-rag-hook` project we
sketched at `~/github-ra-yavuz/claude-rag-hook/DESIGN.md`. Decide
whether to build the MCP surface inside hydra-llm or keep the two
projects separate before committing to either.

## Friction points worth watching

These do not yet have a fix proposed, but have been hit at least once
and might earn a release if they recur:

- The `i386` apt-update notice on machines with `i386` in dpkg's foreign
  architectures. Harmless but noisy. Pin `arch=amd64,arm64` on the apt
  source line in the install instructions, and consider regenerating the
  apt repo Release file with explicit `Architectures: amd64 arm64`.
- The chat session save line `[session saved]` always prints even when
  the model produced nothing. Could be conditional on whether the
  current turn produced anything new.
- Bulk `hydra-llm addlocal <folder>` derives ids from filenames via a
  slugify; long Hugging-Face-style filenames produce long ugly ids. A
  `--prefix` flag or smarter id derivation might help.
- When the recommended-embedder download is interrupted partway through
  (Ctrl+C, network drop), the .part file is left behind and the next
  attempt re-downloads from scratch. `wget -c`-style resume would be a
  nice-to-have on the slow wide-area links some users will hit.

## What stays explicitly out of scope

These have been considered and consciously left out:

- Per-user telemetry of any kind. The privacy stance is non-negotiable.
- Auto-update of the deb itself. apt is the update mechanism.
- A model-decides "RAG tool" that the chat model can call (versus the
  current always-on-when-flag-set RAG). The LLM round-trip cost of the
  tool-call dance does not pay off at personal scale; the explicit-flag
  surface is cheaper and equally useful.
- A "managed" cloud component. hydra-llm is local-first by design.
