# Releasing and the Changelog page

Versions, release notes, tags, Docker image tags and the **Changelog** page (**Global Settings → Changelog**) are all
driven by git: by the **commit messages** you and your contributors write. Nothing is typed in twice.

| What | Where it comes from |
| ---- | ------------------- |
| Version number | The nearest git release tag (`v1.5.0` → `1.5.0`). The `MAJOR.MINOR.PATCH` bump is chosen from the keywords of the commits since the last release |
| Release notes | The commits since the previous release, grouped by keyword (`tools/release_notes.py`) |
| Build identity | `1.5.0` = exactly that release; `1.5.0+5.gabc1234` = 5 commits after it (a development build) |
| What the next release will contain | The **Unreleased** card on the Changelog page of any image built from a branch that is ahead of the last release |
| Version in a downloaded source zip | The root `VERSION` file, which changes **only when a new major version is released**, shown as `2.0.0+src` |

## How it fits together

Changes reach `TSDB_dev` in two ways, and both are read the same way because both end up as **commits with a keyword**:

``` txt
 you:          git commit -m "feat: Add Bluetooth functionality"      ──┐
                                                                         ├─►  TSDB_dev
 contributor:  pull request titled "feat: Add an Acme register map"   ──┘     (a squash-merged pull request's
               squash-merged; the PR title becomes the commit message           title is its commit message)
                                   │
        private image from TSDB_dev: the Changelog page shows an "Unreleased" card
        (the changes since the last release and the version they will get)
                                   │  you merge TSDB_dev into master (a merge commit)
                                   ▼
        Release workflow ── mode "preview": shows the proposed version and notes
                         └─ mode "publish": creates the GitHub release and the tag vX.Y.Z
                                   │
        Publish Docker Public Image (by hand) ─► image tags latest, <date> and X.Y.Z
```

## Keywords

The keyword at the start of a commit message (or pull request title) decides the **section** of the release notes and
the **version bump**. The highest bump among all the commits wins.

| Keyword | Section | Bump (from `1.4.2`) |
| ------- | ------- | ------------------- |
| `feat!:` `fix!:` `chore!:` (any keyword followed by `!`) or `breaking:` | Breaking Changes | major → `2.0.0` |
| `feat:` | Features | minor → `1.5.0` |
| `fix:` `perf:` | Bug Fixes | minor → `1.5.0` |
| `patch:` (a **very minor** bug fix) | Bug Fixes | patch → `1.4.3` |
| `chore:` `build:` `ci:` `test:` `refactor:` `style:` | Maintenance | patch → `1.4.3` |
| `docs:` | Documentation | patch → `1.4.3` |
| no keyword | Other Changes | minor → `1.5.0` |

* An optional scope is allowed: `feat(modbus): Add an Acme register map`.
* Choose `fix:` for a bug fix that deserves a new minor version and `patch:` for one that does not (a typo in a log
  message, a small guard clause). The `1.4.x` releases are routine work and very minor fixes only.
* A line starting `BREAKING CHANGE:` in a commit's body also makes that commit a major change.
* A message without a keyword is still listed (under Other Changes) and asks for a minor bump, so nothing is under-bumped.
  Use `--bump` / `version` on the Release workflow if that is not what you want.
* Add `[skip changelog]` to the message or the pull request title to leave a change out of the release notes. Merge
  commits are ignored automatically (the commits they bring in are counted instead).
* **Dependabot** needs nothing from you: its titles are `chore(deps): ...`, `chore(ci): ...` and `chore(docker): ...`
  (set in `.github/dependabot.yml`), so they are listed under Maintenance and ask for a patch bump.
* The highest bump wins, so a single `feat:` among twenty Dependabot updates makes the next release `1.5.0`.

Each entry in the notes shows the title, the pull request (when there is one), the author and the commit:

``` txt
## v1.5.0 (2026-11-25)

### 🚀 Features

* Add an Acme register map ([#42](https://github.com/BuxtonCalvin/MultiProtocolGateway/pull/42) by [@alice](https://github.com/alice), `2476c4b`)
* Add Bluetooth functionality (by [@BuxtonCalvin](https://github.com/BuxtonCalvin), `dafb50d`)

### 🧰 Maintenance

* bump requests from 2.31 to 2.32 ([#51](https://github.com/BuxtonCalvin/MultiProtocolGateway/pull/51) by [@dependabot[bot]](https://github.com/apps/dependabot), `078ee22`)

**Detailed Changes**: [v1.4.2...v1.5.0](https://github.com/BuxtonCalvin/MultiProtocolGateway/compare/v1.4.2...v1.5.0)
```

Entries are sorted by title within a section. The author comes from the commit (its GitHub no-reply address, or a lookup
through the GitHub API when the workflow runs).

## Your routine in VS Code

1. **Code and test locally** as you do now (strict typing, `ruff`, run the app).
2. **Commit with a keyword.** In the Source Control view (`Ctrl+Shift+G`) type the message in the box, for example
   `feat: Add Bluetooth functionality`, and press `Ctrl+Enter`. Commit to `TSDB_dev` and push (**Sync Changes**).
   * One commit is one entry in the notes. Group related work into one commit, or put `[skip changelog]` on follow-up
     commits (`fix: typo in the Bluetooth help text [skip changelog]`).
3. **Run Publish Docker Private Image** (GitHub → Actions, or the GitHub Actions extension). The image reports a version
   such as `1.4.2+5.gabc1234` and its Changelog page has an **Unreleased** card listing those five commits and the
   version they will get.
4. **Pull and run the container** and test for a day.
5. **Preview the release** (optional): Actions → **Release** → *Run workflow*, branch `TSDB_dev`, mode `preview`. The run
   summary shows the proposed version, the bump and the notes. Nothing is published.
6. **Merge `TSDB_dev` into `master` with a merge commit**, not a squash. A squash would collapse all your commits into
   one entry. Either use a pull request (**Create a merge commit**) or merge locally (*Git: Merge Branch…*, then push).
7. **Publish the release:** Actions → **Release** → *Run workflow*, branch `master`, mode `publish`. This creates the
   GitHub release and the tag `vX.Y.Z` with the notes, and updates `VERSION` when the release is a new major version.
8. **Publish the public image** when the changes are substantial: Actions → **Publish Docker Public Image**. Do it
   after step 7 so the image gets the clean version tag (`1.5.0`) next to `latest` and the date.
9. In VS Code run *Git: Pull* so your clone sees the new tag.

The Release workflow only publishes from `master`. From any other branch use `preview`.

## Pull requests from other people

Contributors fork the repository and open a pull request. For their changes to be read exactly like your commits:

1. **Set the merge button once** (repository **Settings → General → Pull Requests**): allow *squash merging* and set
   its default message to the **pull request title**. The title becomes the commit message the notes are built from.
   Keep *merge commits* enabled for your own `TSDB_dev` → `master` merge.
2. **The title needs a keyword.** The pull request template (`.github/pull_request_template.md`) lists them, and the
   **PR title** workflow (`pr-title.yml`) shows a red check that explains the keywords when the title is missing one. It
   runs again when the title is edited. It does not block the merge unless you make it a required check
   (**Settings → Rules** or branch protection). You can also just rename the title before merging.
3. **Breaking changes** use `!` in the title (`feat!: ...`) or a `BREAKING CHANGE:` line in the squash commit message.
4. Merge with **Squash and merge**. The pull request number is added to the commit, so the entry links to the pull
   request and credits its author automatically.

The test workflows (`python-3.x.yml`, CodeQL) already run for pull requests into `TSDB*` branches. Ask contributors to
target `TSDB_dev`, not `master`.

## The Release workflow

**Actions → Release → Run workflow.** (GitHub lists a workflow in the Actions tab once its file is on the default branch,
so merge `release.yml` into `master` first.)

| Input | Meaning |
| ----- | ------- |
| `mode` | `preview` (default) shows the proposed version and notes in the run summary and publishes nothing; `publish` creates the GitHub release and tag |
| `bump` | `auto` (default) uses the highest keyword; `major` / `minor` / `patch` overrides it |
| `version` | Optional exact version such as `2.0.0` (no leading `v`). It must be higher than the last release and not already tagged |

What it does, in order: reads the commits since the last release tag, works out the next version, writes the notes
(shown in the run summary), and, in `publish` mode from `master`, runs `gh release create` with that tag and notes,
then updates `VERSION` and commits it as `chore: set VERSION to 2.0.0 [skip changelog]` when the release is a new
major version. If there is nothing releasable since the last release, it says so and stops.

You can run the same logic on your own machine, with no network, to see what a release would contain:
`python tools/release_notes.py --no-enrich` (add `--bump patch` or `--version 2.0.0` to try overrides).

## What the Changelog page shows

**Global Settings → Changelog** is a collapsible outline (use *Expand all* / *Collapse all*, or link straight to a release
with `/pages/changelog#release-1.5.0`):

| Level | Example | What is inside |
| ----- | ------- | -------------- |
| Unreleased | `Unreleased, next release 1.5.0 (minor bump)` | The commits since the last release (only on a build that is ahead of it) |
| Series | `Version 2.x` | Every release of that major version |
| Release | `Version 2.1.0` | Date, `Tag`, `Commit`, `Released by`, a *Detailed changes* compare link |
| Section | `Bug Fixes (3)` | The entries of that category |
| Entry | `Fix reconnect message (#42 by @alice, 0123abc)` | The PR number links to the pull request, the author to their profile, the hash to the commit |

The newest series, the newest release, the installed release and their sections start open. The history is captured
when the Docker image is built (`tools/generate_changelog.py` reads the published GitHub Releases and the commits since
the last one) and ships inside the image, because a running container has no `.git` folder and may have no network.

## Starting point

The repository already has a published release, **`v1.0.0`**. The next release collects every commit since it and
proposes the next number. Until you publish a newer release, the Changelog page lists only `1.0.0`.

**Do not fetch tags from the upstream project.** This repository is a fork of `HotNoob/PythonProtocolGateway`; its tags
are unrelated and would corrupt `git describe`. Use `git fetch upstream --no-tags` if you ever fetch from it.

## How many releases? (and what uses storage)

**You decide how many releases exist: one is created each time you run the Release workflow in `publish` mode, never
automatically.** The commits simply accumulate, and the notes of the release you eventually publish describe everything
since the previous one. The Changelog page, the Docker version tag and the zip's `VERSION` all follow the releases you publish.

* **Major releases only (`1.0.0`, `2.0.0`):** wait until a breaking change is in, run `preview` (it proposes `2.0.0`),
  then `publish`.
* **Minor releases (`1.1.0`, `1.2.0`):** publish whenever you want a deliverable. A feature or a bug fix asks for a minor bump.
* **Patch releases (`1.4.1`, `1.4.2`):** routine work only: chores, Dependabot updates, tests, docs and very minor fixes
  (`patch:`).

What does *not* use meaningful storage: **git tags and GitHub Releases** (tiny pointers and text), the **"Source code
(zip / tar.gz)"** links on a release (generated by GitHub on demand; this project attaches no files), and **extra Docker
tags** (`latest`, the date, the version are labels on one image).

What does: every separate image *build* you push adds layers to Docker Hub. Both image workflows are run by hand, so you
decide how many builds there are. Check Docker Hub's current retention rules for your plan if space matters there.

## Source zip downloads (GitHub "Download ZIP")

A zip of the source contains no `.git` folder, so git cannot supply the version. Instead the app reads the `VERSION` file
at the project root. To avoid a commit for every release, that file changes **only for major versions**:

| Release published | `VERSION` |
| ----------------- | --------- |
| `v1.4.2` → `v1.5.0` (patch or minor) | unchanged |
| `v1.5.0` → `v2.0.0` (major) | set to `2.0.0` |
| `v2.0.0-rc1` (pre-release) | never changed |
| a fix for an older major, e.g. `v1.9.9` after `v2.0.0` | never moves backwards |

A zip therefore reports the last major release it is based on, with a `+src` suffix (`2.0.0+src`), and the Changelog page
says it may contain later changes. Docker images and git clones are unaffected: they report the exact version.

The Release workflow updates `VERSION` itself right after it publishes (a release created with the workflow token does
not start other workflows). For a release you create by hand on the GitHub Releases page, `update-version-file.yml` does
the same. The commit is marked `[skip changelog]` so it never appears in the next release's notes. The release's own tag
points at the commit *before* it, so the "Source code (zip)" on the release page has the old `VERSION`; **Code → Download
ZIP on `master`** has the new one. If `master` is protected (pull request required), the push is rejected: allow the
`github-actions` bot to bypass the rule, or edit `VERSION` by hand in a pull request. Preview the decision locally with
`python tools/update_version_file.py --tag v2.0.0 --dry-run`.

## Trying things on your development machine

When you run MPG from a clone, the **version** is read live from git (`git describe`). The **release history** needs
`classes/WebServer/changelog.json`, which is generated and not committed (it is in `.gitignore`).

Create `.vscode/tasks.json` (the `.vscode` folder is gitignored, so this stays local):

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "MPG: Generate changelog",
      "type": "shell",
      "command": "${command:python.interpreterPath} tools/generate_changelog.py",
      "problemMatcher": []
    },
    {
      "label": "MPG: Preview release notes",
      "type": "shell",
      "command": "${command:python.interpreterPath} tools/release_notes.py --no-enrich",
      "problemMatcher": []
    },
    {
      "label": "MPG: Show version",
      "type": "shell",
      "command": "${command:python.interpreterPath} -c \"from defs.app_version import app_version; print(app_version())\"",
      "problemMatcher": []
    },
    {
      "label": "MPG: Test releases and changelog",
      "type": "shell",
      "command": "${command:python.interpreterPath} -m pytest pytests/test_changelog.py pytests/test_release_notes.py -q",
      "problemMatcher": []
    }
  ]
}
```

Run them with **Terminal → Run Task…**. Besides the list of releases, the generator makes one GitHub API request per commit
or pull request whose author or hash it has to look up. Anonymous requests are limited to 60 per hour, so on your own
machine set `GITHUB_TOKEN` first (for example `export GITHUB_TOKEN=$(gh auth token)`), or add `--no-enrich`. The workflows
pass the built-in token. Add `--no-unreleased` to leave out the Unreleased section. Regenerating the file is picked up by
the running app without a restart.

## Troubleshooting

| Symptom | Cause and fix |
| ------- | ------------- |
| Changelog page says `changelog.json was not found` | The file is generated at build time. Run the *MPG: Generate changelog* task, or use a Docker image built by the workflows. A plain local `docker build` does not generate it |
| Page says *No releases have been published yet* | Only **published** GitHub releases are listed. Run the Release workflow in `publish` mode |
| No **Unreleased** card on a private image | The image was built from a commit with nothing releasable after the last tag (or a checkout without tags). The Docker workflows check out the full history; if you build another way, fetch tags first |
| Version shows `0.0.0+g…` | No release tag is reachable. Run *Git: Fetch*; confirm `git tag` lists `v1.0.0` |
| Version shows an unrelated number | Tags from the upstream fork source were fetched. Delete them (`git tag -d <tag>`) and fetch with `--no-tags` |
| A commit is missing from the notes | It contains `[skip changelog]`, is a merge commit, or was made before the last release tag. Preview shows exactly what is counted |
| A commit is under *Other Changes* | Its message has no keyword. Rewrite it next time; for the upcoming release use `bump` / `version` on the workflow if the proposed number is wrong |
| The release proposes the wrong version | A commit asked for a bigger or smaller bump than you intended. Run `preview`, then use the `bump` or `version` input when publishing |
| A contributor's change shows no PR number or author | It was merged with a merge commit or by rebasing instead of **Squash and merge**. The commit is still listed, under its own message |
| The PR title check is red | The title does not start with a keyword. Edit the title (the check re-runs); the message in the run summary lists the keywords |
| Release workflow says "Publishing is only allowed from master" | `publish` was run from another branch. Run it from `master`, or use `preview` |
| `VERSION` did not change after a major release | Check the *Commit VERSION* step of the Release run: `master` must accept the bot's push |
| A zip shows `…+src` or an older major than expected | By design: the zip's `VERSION` only moves on major releases. Use Docker or a git clone for the exact version |
| Docker build fails at *Generate changelog* | GitHub API hiccup; re-run the workflow |
