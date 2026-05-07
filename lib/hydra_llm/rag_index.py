"""File walker, code/prose classifier, and line-aware chunker for hydra RAG.

Pure logic, no embedding or storage. Inputs are paths; outputs are dicts of
file metadata and chunk records ready to hand to the LanceDB store.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

try:
    import pathspec
    HAVE_PATHSPEC = True
except ImportError:
    HAVE_PATHSPEC = False


# Files we never want in any index, regardless of .gitignore.
BUILTIN_IGNORE_DIRS = (
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor",
    ".venv", "venv", "env", "__pycache__", ".tox", ".mypy_cache", ".ruff_cache",
    "target", "dist", "build", "out", ".next", ".nuxt", ".cache",
    ".gradle", ".idea", ".vscode",
    ".terraform", ".pulumi",
    ".hydra-index",
)
BUILTIN_IGNORE_GLOBS = (
    "*.lock", "*.lockb", "package-lock.json", "yarn.lock", "Cargo.lock",
    "*.min.js", "*.min.css", "*.map",
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dylib", "*.dll", "*.a", "*.o",
    "*.class", "*.jar", "*.war",
    "*.gguf", "*.bin", "*.safetensors", "*.pt", "*.pth", "*.onnx",
    "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.rar", "*.7z",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.bmp", "*.ico", "*.tiff",
    "*.mp3", "*.mp4", "*.mov", "*.avi", "*.mkv", "*.flac", "*.wav", "*.ogg",
    "*.pdf", "*.doc", "*.docx", "*.ppt", "*.pptx", "*.xls", "*.xlsx",
    "*.sqlite", "*.db", "*.dbf",
    "*.parquet", "*.arrow", "*.feather",
    ".DS_Store", "Thumbs.db",
)

# Code extensions: trigger the code embedder. Anything not on this list and
# not on PROSE_EXTS gets sniffed by content for the final decision.
CODE_EXTS = frozenset({
    ".py", ".pyi", ".pyx",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".kts", ".scala",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
    ".cs", ".fs", ".fsx", ".vb",
    ".rb", ".php", ".pl", ".pm", ".lua",
    ".swift", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1",
    ".sql", ".graphql", ".gql",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte", ".astro",
    ".dart", ".elm", ".clj", ".cljs", ".cljc", ".edn",
    ".ex", ".exs", ".erl", ".hrl",
    ".hs", ".lhs", ".purs",
    ".ml", ".mli", ".mll", ".mly",
    ".nim", ".zig", ".v", ".vala",
    ".tf", ".tfvars", ".hcl", ".nomad",
    ".dockerfile", ".containerfile",
    ".cmake", ".bzl", ".mk",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".jsonc", ".json5",
    ".proto", ".thrift", ".avsc", ".capnp",
    ".feature",
})

PROSE_EXTS = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".text", ".rdoc", ".tex",
    ".org", ".adoc", ".asciidoc",
    ".html",  # ambiguous; if no scripty content it's prose
    "",       # extensionless: README, LICENSE, etc.
})

# Filenames (case-insensitive) that should always be prose regardless of ext.
PROSE_BASENAMES = frozenset({
    "readme", "license", "licence", "copying", "authors", "contributors",
    "changelog", "changes", "history", "news", "todo", "notes",
    "code_of_conduct", "contributing", "security", "support",
    "install", "installation", "usage", "manual",
})

# Basenames (case-insensitive, with or without ext) that are code regardless
# of extension or canonical-prose-name resemblance.
CODE_BASENAMES = frozenset({
    "makefile", "gnumakefile", "rakefile", "gemfile", "guardfile", "podfile",
    "vagrantfile", "berksfile", "dockerfile", "containerfile", "jenkinsfile",
    "build", "build.bazel", "workspace", "workspace.bazel",
    "build.gradle", "settings.gradle", "build.sbt",
    ".bazelrc", ".clang-format", ".clang-tidy", ".editorconfig",
    ".eslintrc", ".prettierrc", ".gitattributes",
    "configure", "configure.ac", "configure.in",
    "pyproject", "setup", "manifest",
})


@dataclass
class FileInfo:
    path: str          # absolute, resolved
    rel_path: str      # path relative to walk root
    size: int
    mtime: float
    kind: str          # "code" | "prose" | "skip"
    reason: str = ""   # why kind was assigned (for debug)


@dataclass
class Chunk:
    file_path: str       # absolute path of source file
    rel_path: str        # relative to walk root
    kind: str            # "code" | "prose"
    chunk_idx: int       # 0-based ordinal within the file
    byte_start: int
    byte_end: int
    text: str
    line_start: int
    line_end: int
    mtime: float = 0.0
    size: int = 0


@dataclass
class WalkSummary:
    files_total: int = 0
    files_kept: int = 0
    files_ignored: int = 0
    files_too_large: int = 0
    files_binary: int = 0
    bytes_total: int = 0
    code_count: int = 0
    prose_count: int = 0
    ignore_reasons: dict = field(default_factory=dict)


def _matches_any_glob(rel_path: str, globs: Iterable[str]) -> bool:
    """Glob match against the basename and full relative path."""
    from fnmatch import fnmatch
    base = os.path.basename(rel_path)
    for g in globs:
        if fnmatch(base, g) or fnmatch(rel_path, g):
            return True
    return False


def _load_gitignore(root: Path):
    """Return a pathspec.PathSpec or None if root has no .gitignore (or
    pathspec isn't available). Combines all .gitignore files we find in the
    tree by treating them as a single git-style pathspec with paths relative
    to root.
    """
    if not HAVE_PATHSPEC:
        return None
    patterns: list[str] = []
    for gi in root.rglob(".gitignore"):
        try:
            sub = gi.parent.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            text = gi.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # If the .gitignore is in a subdir, prepend that subdir so the
            # pattern is interpreted relative to root.
            if sub == "." or sub == "":
                patterns.append(stripped)
            else:
                # Negation handling: keep the leading ! when scoping.
                if stripped.startswith("!"):
                    patterns.append("!" + sub + "/" + stripped[1:])
                else:
                    patterns.append(sub + "/" + stripped)
    if not patterns:
        return None
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _looks_binary(p: Path, sniff_bytes: int = 8192) -> bool:
    """Quick binary sniff: NUL byte in the first chunk = binary."""
    try:
        with open(p, "rb") as f:
            buf = f.read(sniff_bytes)
    except OSError:
        return True
    if b"\x00" in buf:
        return True
    # Also reject if too many high-bit non-UTF8 bytes in a row.
    try:
        buf.decode("utf-8")
    except UnicodeDecodeError:
        # Try latin-1 as a fallback; if nothing decodes, treat as binary.
        try:
            buf.decode("latin-1")
        except UnicodeDecodeError:
            return True
        # Very high ratio of non-printables = probably binary even if it
        # decodes as latin-1.
        printable = sum(1 for b in buf if 32 <= b < 127 or b in (9, 10, 13))
        if buf and printable / len(buf) < 0.7:
            return True
    return False


def _classify(p: Path, basename_lower: str, ext: str) -> tuple[str, str]:
    """Return (kind, reason) where kind is 'code' | 'prose'.

    Rule order (most-specific first):
      1. Explicit code extension wins (.sh, .py, ...).
      2. Code-canonical basename wins (Makefile, Dockerfile, ...).
      3. Shebang sniff -> code.
      4. Prose-canonical basename (README, LICENSE, ...).
      5. Explicit prose extension (.md, .txt, ...).
      6. Default: prose.
    """
    stem_lower = os.path.splitext(basename_lower)[0]
    # 1. Code extensions win over basename heuristics so install.sh isn't
    #    misclassified as prose just because its stem is "install".
    if ext in CODE_EXTS:
        return "code", f"ext:{ext}"
    # 2. Canonical code basenames (Makefile, Dockerfile, etc.).
    if basename_lower in CODE_BASENAMES or stem_lower in CODE_BASENAMES:
        return "code", "canonical-code-basename"
    # 3. Shebang.
    try:
        with open(p, "rb") as f:
            first = f.read(2)
        if first == b"#!":
            return "code", "shebang"
    except OSError:
        pass
    # 4. Canonical prose basenames (README, LICENSE, etc.).
    if stem_lower in PROSE_BASENAMES:
        return "prose", "canonical-prose-basename"
    # 5. Explicit prose extension.
    if ext in PROSE_EXTS:
        return "prose", f"ext:{ext}"
    # 6. Default. Code-fenced blocks in markdown already cover the case
    #    where prose contains code samples, so prose is the safer default.
    return "prose", "unknown-ext-default-prose"


def walk_folder(root: str | os.PathLike,
                *,
                max_file_size_bytes: int = 1 * 1024 * 1024,
                extra_excludes: Iterable[str] = (),
                extra_includes: Iterable[str] = (),
                max_depth: int | None = None,
                use_gitignore: bool = True) -> tuple[list[FileInfo], WalkSummary]:
    """Walk `root`, return (files_kept, summary).

    `extra_excludes` / `extra_includes` are git-wildcard-style patterns
    layered on top of .gitignore + builtin blacklist. An include pattern
    overrides any exclude (built-in or user) for that file.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    summary = WalkSummary()
    kept: list[FileInfo] = []

    git_spec = _load_gitignore(root) if use_gitignore else None
    extra_excl_spec = (pathspec.PathSpec.from_lines("gitwildmatch", list(extra_excludes))
                       if (HAVE_PATHSPEC and extra_excludes) else None)
    extra_incl_spec = (pathspec.PathSpec.from_lines("gitwildmatch", list(extra_includes))
                       if (HAVE_PATHSPEC and extra_includes) else None)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dpath = Path(dirpath)
        try:
            rel_dir = dpath.relative_to(root)
        except ValueError:
            continue
        depth = 0 if rel_dir == Path(".") else len(rel_dir.parts)
        if max_depth is not None and depth > max_depth:
            dirnames[:] = []
            continue
        # Prune builtin-ignored dir names in-place (os.walk respects this).
        dirnames[:] = [d for d in dirnames if d not in BUILTIN_IGNORE_DIRS]

        for name in filenames:
            full = dpath / name
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            summary.files_total += 1

            # Includes win over excludes. If the user explicitly whitelisted
            # this file, skip exclude checks.
            forced_in = bool(extra_incl_spec and extra_incl_spec.match_file(rel))

            if not forced_in:
                if _matches_any_glob(rel, BUILTIN_IGNORE_GLOBS):
                    summary.files_ignored += 1
                    summary.ignore_reasons["builtin-glob"] = summary.ignore_reasons.get("builtin-glob", 0) + 1
                    continue
                if extra_excl_spec and extra_excl_spec.match_file(rel):
                    summary.files_ignored += 1
                    summary.ignore_reasons["user-exclude"] = summary.ignore_reasons.get("user-exclude", 0) + 1
                    continue
                if git_spec and git_spec.match_file(rel):
                    summary.files_ignored += 1
                    summary.ignore_reasons["gitignore"] = summary.ignore_reasons.get("gitignore", 0) + 1
                    continue

            try:
                st = full.stat()
            except OSError:
                summary.files_ignored += 1
                summary.ignore_reasons["stat-failed"] = summary.ignore_reasons.get("stat-failed", 0) + 1
                continue
            if st.st_size > max_file_size_bytes:
                summary.files_too_large += 1
                summary.ignore_reasons["too-large"] = summary.ignore_reasons.get("too-large", 0) + 1
                continue
            if st.st_size == 0:
                summary.files_ignored += 1
                summary.ignore_reasons["empty"] = summary.ignore_reasons.get("empty", 0) + 1
                continue
            if _looks_binary(full):
                summary.files_binary += 1
                summary.ignore_reasons["binary"] = summary.ignore_reasons.get("binary", 0) + 1
                continue

            ext = full.suffix.lower()
            kind, reason = _classify(full, name.lower(), ext)
            kept.append(FileInfo(
                path=str(full),
                rel_path=rel,
                size=st.st_size,
                mtime=st.st_mtime,
                kind=kind,
                reason=reason,
            ))
            if kind == "code":
                summary.code_count += 1
            else:
                summary.prose_count += 1
            summary.bytes_total += st.st_size
            summary.files_kept += 1

    return kept, summary


def chunk_file(file_info: FileInfo,
               *,
               target_chars: int = 1500,
               overlap_chars: int = 200) -> list[Chunk]:
    """Split a file into overlapping chunks. Line-aware: never breaks a line.

    target_chars is a soft cap (we'll keep going to the end of the current
    line/paragraph). overlap_chars is approximate -- we walk back N chars and
    snap to the nearest line boundary.
    """
    try:
        with open(file_info.path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    if not text:
        return []

    chunks: list[Chunk] = []
    start = 0
    n = len(text)
    chunk_idx = 0
    # Pre-build a list of line-start byte offsets so we can map char->line.
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def line_of(char_pos: int) -> int:
        # Binary-search line_starts for the largest i such that line_starts[i] <= char_pos.
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= char_pos:
                lo = mid
            else:
                hi = mid - 1
        return lo  # 0-indexed; +1 for human-readable

    while start < n:
        soft_end = min(start + target_chars, n)
        # Snap soft_end forward to the next newline so we don't split lines.
        end = soft_end
        if end < n:
            nl = text.find("\n", end)
            if nl == -1:
                end = n
            else:
                end = nl + 1  # include the newline
        snippet = text[start:end]
        if snippet.strip():
            l_start = line_of(start) + 1
            l_end = line_of(max(end - 1, start)) + 1
            chunks.append(Chunk(
                file_path=file_info.path,
                rel_path=file_info.rel_path,
                kind=file_info.kind,
                chunk_idx=chunk_idx,
                byte_start=start,
                byte_end=end,
                text=snippet,
                line_start=l_start,
                line_end=l_end,
                mtime=file_info.mtime,
                size=file_info.size,
            ))
            chunk_idx += 1
        # Advance, with overlap.
        if end >= n:
            break
        next_start = max(end - overlap_chars, start + 1)
        # Snap next_start back to a line boundary.
        nl_back = text.rfind("\n", 0, next_start)
        if nl_back >= 0 and nl_back + 1 > start:
            next_start = nl_back + 1
        start = next_start
    return chunks


def chunk_all(files: Iterable[FileInfo], **kw) -> Iterator[Chunk]:
    """Yield chunks across many files. Lazy so callers can stream."""
    for f in files:
        yield from chunk_file(f, **kw)
