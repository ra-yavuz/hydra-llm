# Deploying a new release of hydra-llm

Releases are triggered by **pushing a tag** of the form `vX.Y.Z`. CI does the rest: builds the .deb(s) and the .plasmoid bundle, attaches them to a GitHub Release, and auto-publishes the .deb(s) to the [ra-yavuz apt repository](https://github.com/ra-yavuz/apt). No manual download or commit step.

## Pre-flight checklist

1. **Bump the version**:
   - `debian/changelog` &rarr; new entry `hydra-llm (X.Y.Z-1) unstable; urgency=low` with notes
   - `plasmoid/metadata.json` &rarr; `"Version": "X.Y.Z"`
   - any version constants in source if applicable
2. **Verify lint passes locally**:
   ```bash
   make lint
   ```

## Ship it

```bash
git add -A
git commit -m "vX.Y.Z: <one-line summary>"
git push
git tag -a vX.Y.Z -m "vX.Y.Z: <one-line summary>"
git push origin vX.Y.Z
```

CI takes ~3 minutes:

- **lint** + **build-deb** &rarr; produces `dist/hydra-llm*_X.Y.Z-1_*.deb` (this repo ships multiple .deb packages: the daemon plus the plasmoid frontend) and `dist/hydra-llm.plasmoid`
- **release** (tag-only) &rarr; creates the GitHub Release, attaches all .deb files and the .plasmoid, dispatches one `package-published` event per .deb to `ra-yavuz/apt`
- The apt repo's **add-package** workflow downloads each, places it under the right pool path, evicts older versions, commits, pushes
- The push triggers **publish** which rebuilds the apt index

Within ~5 min total, `sudo apt update && sudo apt install hydra-llm` serves the new versions.

## What if the tag run fails?

Watch CI at https://github.com/ra-yavuz/hydra-llm/actions. Common causes:

- **shellcheck or py_compile errors**: re-run `make lint` and fix.
- **release upload `403 Resource not accessible`**: the `release` job needs `permissions: contents: write` (already set).
- **apt dispatch silently skipped**: the step requires the `APT_DISPATCH_TOKEN` secret to exist on this repo. Check `Settings &rarr; Secrets and variables &rarr; Actions`.

To retry after a fix: delete and re-push the tag.

```bash
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z
# fix the issue, commit, push, retag
git tag -a vX.Y.Z -m "vX.Y.Z: ..."
git push origin vX.Y.Z
```

## What gets published

| Surface | URL | Updated by |
|---|---|---|
| GitHub Release | `https://github.com/ra-yavuz/hydra-llm/releases/tag/vX.Y.Z` | `release` job |
| .deb in apt repo | `https://ra-yavuz.github.io/apt/pool/main/h/hydra-llm/` and `.../h/hydra-llm-plasma/` | `add-package` &rarr; `publish` |
| Apt index | `https://ra-yavuz.github.io/apt/dists/stable/main/binary-amd64/Packages` | `publish` |
| Project Pages | `https://ra-yavuz.github.io/hydra-llm/` | redeploys on any push to `main` |

## Same flow lives in herald, inhibit-charge, meowtrics

The four packaged repos share the same CI shape (`lint &rarr; build-deb &rarr; release`) and the same dispatch pattern.
