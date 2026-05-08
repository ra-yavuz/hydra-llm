# hydra-llm roadmap

Things deferred from past releases or noticed during use that haven't been
shipped yet. Not promises, not deadlines: just a written list so the next
working session has somewhere to start. Items roughly ordered by how much
real friction they remove for the user.

## Open

### Plasma widget surfaces RAG state

The Plasma widget currently shows chat-model rows. It does not show
indexed folders, embedder running state, or RAG-bound bundles
(despite tray.py already exposing `rag_index` per row). A "RAG" tab
or section in the popup would let users:

- See which folders they have indexed
- One-click reindex
- See which embedder containers are running
- Stop them with one click

Out of scope for the current code-side releases because the tray-side
data is already exposed; this is purely a QML/UI add. Worth doing once
the Plasma 6 surface gets another pass.

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

### SHA256 backfill of the catalogs

The downloader honors `sha256` on every catalog entry (see v0.2.6) and
the backfill script `scripts/sha256-backfill.py` is in place. The
field still has to be populated by running the script once, reviewing
the diff, and committing it. Deferred from v0.2.6 because it's a
one-shot data-entry operation, not a code change.

## On "single embeddings DB with buckets per folder"

Asked once, written down so the answer survives. The current design is
N per-folder LanceDB indexes plus one global JSON registry of paths and
tags. A user can scope to one folder, several folders, a tag set, or
everything globally on any chat or query call via
`--rag` / `--rag-stores` / `--rag-tag` / `--rag-all` / `--rag-collection`.
The "buckets, optionally global" surface already exists.

We are sticking with this layout rather than collapsing to a single
multi-tenant DB because:

- The index lives next to the data. Move, rename, or delete a folder
  and the index follows. A monolithic DB needs a janitor.
- Different folders can use different embedders (a code project on
  qwen3-embed-4b, a novel on nomic-embed-text). One LanceDB table
  cannot mix dimensions cleanly.
- Per-folder LanceDB scales fine at personal scale; federation is
  "open N tables, merge top-K", which is cheap up to hundreds of
  stores. A single 10M-row table would still need a `WHERE folder=...`
  filter to be useful, which is buckets-with-extra-steps.
- Permission boundaries stay clean. A per-folder index can sit on an
  external drive or in an encrypted home subtree without dragging the
  whole catalog with it.

The discoverability problem (users not knowing federation exists) is
solved by `hydra-llm rag explain`, not by changing storage.

## Friction points worth watching

These do not yet have a fix proposed, but have been hit at least once
and might earn a release if they recur:

- Bulk `hydra-llm addlocal <folder>` derives ids from filenames via a
  slugify; long Hugging-Face-style filenames produce long ugly ids. The
  v0.2.6 `--prefix` flag helps, but a smarter derivation that drops
  redundant family/quant suffixes more aggressively would help further.

## What stays explicitly out of scope

These have been considered and consciously left out:

- Per-user telemetry of any kind. The privacy stance is non-negotiable.
- Auto-update of the deb itself. apt is the update mechanism.
- A model-decides "RAG tool" that the chat model can call (versus the
  current always-on-when-flag-set RAG). The LLM round-trip cost of the
  tool-call dance does not pay off at personal scale; the explicit-flag
  surface is cheaper and equally useful.
- A "managed" cloud component. hydra-llm is local-first by design.
- Collapsing per-folder indexes into one multi-tenant DB. See the
  dedicated section above for why.
