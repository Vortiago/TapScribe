---
name: release
description: Cut a TapScribe release. Verify, bump, PR, annotated tag, push. User-invoked only.
disable-model-invocation: true
---

# Cut a release

A release is a deliberate, human-pushed `vX.Y.Z` tag, never automatic
([ADR-0012](../../../docs/adr/0012-bridge-artifacts-on-tagged-releases.md)).
The tag fires `.github/workflows/release.yml`, which builds and publishes
everything. What that produces is [RELEASING.md](../../../RELEASING.md); this
skill is the ritual that gets there.

**Two hard stops below are dual-control.** Merging to main and pushing the tag
are the operator's, never yours. Stop and hand over.

## 1. Verify before touching anything

```bash
git fetch --all --tags --prune
git tag --sort=-v:refname | head -3          # the latest release
grep '^version' pyproject.toml               # what main currently declares
git log --oneline <latest-tag>..origin/main  # what would ship
```

**The trap:** if `pyproject.toml` still declares the *previous* version, tagging
now ships a release stamped with it. The bump has to land on main first, which
is what steps 2 and 3 are for and why they come before the tag.

## 2. Pick the version

```bash
git log <latest-tag>..origin/main --format='%s' | grep '!'      # breaking subjects
git log <latest-tag>..origin/main --format='%B' | grep BREAKING # breaking footers
```

Either one non-empty means major. Otherwise a `feat` means minor, and
fixes-only means patch. State the call and its evidence in the PR body. A
judgement call (a dependency dropped, a default flipped) gets named explicitly
so the operator can overrule it.

## 3. The bump, as its own PR

```bash
python tools/bump_version.py X.Y.Z
git switch -c release-vX.Y.Z origin/main
git commit -am 'chore(release): vX.Y.Z'
git push -u origin release-vX.Y.Z
gh pr create --base main --title 'chore(release): vX.Y.Z' --body-file <file>
```

- The title is **`chore(release):`**. Every real one has been; `pr-title.yml`
  would also accept `build`, but do not start a second convention.
- `bump_version.py` stamps `pyproject.toml`, `tapscribe/__init__.py` and
  `bridges/spacialchat-bridge/manifest.json` in lock-step, and
  `tests/test_version_consistency.py` fails on drift. Never hand-edit the three.
- Version strings in `tests/` and `BundleLayoutTests.cs` are deliberately
  arbitrary fixtures. Leave them.
- `.claude/settings.json` sets `attribution` empty for both commit and PR:
  **no Claude trailer** in either.

The body carries why this bump level, what ships (grouped by area, each row
naming its PRs), any release-job path running for the first time, and the tag
command.

### STOP: the operator merges

Dual-control. Wait for CI green and for them to merge.

## 4. The tag

**Annotated, never lightweight.** Every existing tag is annotated and its
message is the release notes: a `vX.Y.Z: headline` first line, blank line, then
one bullet per shipped PR as `type(scope): Description (#PR, closes #issue)`.
`git tag -l -n50 v1.2.0` is the shape to match.

Tag the merged commit. It need not be the bump commit itself: v1.2.0 sits on a
later main, which is fine as long as the version on that commit is right.

```bash
git fetch origin main
git tag -a vX.Y.Z <merged-sha> -F <notes-file>
```

### STOP: the operator approves the push

`git push origin vX.Y.Z` is a deploy. It publishes a public GitHub Release, the
wheel to PyPI via Trusted Publishing, and the image to GHCR. Get the word first.

## 5. Watch it land

```bash
gh run watch $(gh run list --workflow=release.yml --limit 1 --json databaseId -q '.[0].databaseId')
```

Confirm every asset [RELEASING.md](../../../RELEASING.md) lists is attached. The
one to watch is any job that has never run on a real tag: the macOS
`.pkg`/`.zip` pair first ran at v1.3.0.

The dashboard's "Get a bridge" card links
`releases/latest/download/<asset>`, so a missing asset is a 404 on the card
rather than a silent gap.

## Never

- Tag before the bump PR is merged.
- Push a lightweight tag.
- Force-push. A pushed commit stands: fix a message with `gh pr edit` (the
  squash-merge takes the PR title and body) and content with a follow-up commit.
- Merge the bump PR or push the tag without the operator saying so.
