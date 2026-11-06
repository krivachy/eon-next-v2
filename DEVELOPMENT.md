# Development Guide

This document is for maintainers and contributors working on the integration.

## Local Setup

Recommended Python version: `3.13`.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install homeassistant basedpyright
```

Install commit tooling:

```bash
npm install
```

## AI Contributor Guidance

This repository keeps agent guidance in:

- `AGENTS.md` (canonical entry point)
- `docs/ai/context.md`
- `docs/ai/conventions.md`
- `docs/ai/workflows.md`
- `docs/ai/checklist.md`

## Project Checks

Run a basic syntax and typing pass before opening a PR:

```bash
python -m compileall custom_components/eon_next
basedpyright -p pyrightconfig.json
python3 .github/scripts/check_release_metadata.py
```

## Home Assistant Development Validation

- Install in a Home Assistant test instance via `custom_components`.
- Confirm config flow works for:
  - First-time login.
  - Re-auth (changed credentials).
- Confirm entity updates for:
  - Meter readings.
  - Daily consumption fallback path.
  - EV smart charging entities (if available on account).

## Commit Conventions

This repository enforces Conventional Commits for semantic versioning.

- Local commits are linted by a Husky `commit-msg` hook using `commitlint`.
- Pull request titles are linted in CI and must also follow Conventional Commit format.
  - This is required because squash merges use the PR title as the final commit subject.
- Commitlint uses `@commitlint/config-conventional` with default ignore behavior and a 100-character header limit.

Examples:

- `feat(sensor): add smart charge status entity`
- `fix(api): handle missing consumption payload`
- `chore(release): 1.2.0`

Allowed types:

- `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`, `test`

## Release Process (HACS-Compatible)

Releases are managed by `release-please` and derived from Conventional Commits.

1. Merge Conventional Commit PRs into `main`.
2. CI updates a draft release PR with:
   - `CHANGELOG.md` updates.
   - `custom_components/eon_next/manifest.json` version bump.
3. Review that draft PR, mark it ready, approve, and merge it when you want to release.
4. The merge triggers tag creation (`vX.Y.Z`) and GitHub Release publication.

Workflow/config files:

- `.github/workflows/release-please.yml`
- `release-please-config.json`
- `.release-please-manifest.json`

HACS discovers new versions from GitHub tags/releases, so release publication is still required.

### HACS release asset

`hacs.json` sets `zip_release: true`, `filename: eon_next.zip` and `hide_default_branch: true`, so HACS installs **only** from an `eon_next.zip` asset attached to a GitHub release. A release without that asset can't be installed or upgraded through HACS.

- `scripts/build_hacs_zip.sh [output]` builds the asset: the contents of `custom_components/eon_next/` at the zip root, excluding `__pycache__` and `*.pyc`.
- `release-please.yml` runs it and uploads the zip when release-please publishes a release.
- `release-asset.yml` does the same for a release published by hand (`release: published`), or on demand via **Actions → Release Asset → Run workflow** with the tag. It refuses to build if `manifest.json` doesn't match the tag.

#### Forks

GitHub disables Actions on new forks, so none of the above runs until you enable it:

1. Open the fork's **Actions** tab and enable workflows.
2. Under **Settings → Actions → General → Workflow permissions**, select **Read and write permissions** and allow GitHub Actions to create and approve pull requests (release-please needs this to open its release PR).

Without Actions, build and attach the zip by hand:

```bash
scripts/build_hacs_zip.sh eon_next.zip
gh release create v2.0.0 eon_next.zip --title v2.0.0 --notes-file <(sed -n '/^## \[2.0.0\]/,/^## \[/p' CHANGELOG.md | sed '$d')
# or, for an existing release:
gh release upload v2.0.0 eon_next.zip --clobber
```

The tag must match `manifest.json`'s `version`.
