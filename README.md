# Floppy - Source-Port & Integration Fork

This repository is our working fork of [dannyvfilms/Floppy](https://github.com/dannyvfilms/Floppy).

The goal is not to maintain a pile of runtime monkey-patches forever. We are taking a known-good customised Floppy installation, based on my local cumulative **2026.09.06 r13 patch baseline**, and moving those changes into the actual Floppy source tree one feature at a time.

The end state should be a normal, maintainable source build of Floppy in which our custom behaviour is implemented cleanly, tested against the current codebase and no longer depends on replacing files inside a running container.

> **Important:** this is a development/integration fork, not the official Floppy distribution. For the upstream project, releases, installation instructions and public documentation, use [dannyvfilms/Floppy](https://github.com/dannyvfilms/Floppy).

---

## What we are doing

Our previous Floppy setup accumulated a sizeable set of accepted local patches. Those patches worked together in the r13 runtime, but simply copying the patched files into a newer source tree would be unsafe: Floppy has continued to evolve, several patches touch the same code, and newer upstream implementations sometimes supersede parts of the old patch logic.

Each patch is therefore being **source-ported**, not copied.

For every feature we:

1. recover the accepted r13 implementation and the original reasoning behind it;
2. review bugs and regressions discovered while the patch was originally developed;
3. compare the patch against current Floppy source;
4. identify overlap with features already ported;
5. preserve newer upstream behaviour where it has replaced or improved the old implementation;
6. add only the behaviour still missing;
7. add regression tests for the historical failure modes;
8. run CI before merging the feature into the integration branch.

This is particularly important for areas such as TV/episode tracking, ratings, History, playback progress, provider identity, Redis-backed state and Kodi, where apparently small changes can have side effects elsewhere in Floppy.

---

## Branch model

`chris/integration` is the integration target for this work.

We do **not** develop features directly on it. Each logical patch gets its own feature branch and pull request:

```text
upstream/current Floppy
        │
        ▼
chris/integration
        │
        ├── feature/patch-a ── PR ──┐
        ├── feature/patch-b ── PR ──┤
        └── feature/patch-c ── PR ──┘
                                     │
                                     ▼
                              chris/integration
```

A feature PR stays **Draft** while it is being developed. Draft PRs receive a fast targeted CI pass. When the port is complete, the PR is marked **Ready for review**, which triggers the full regression suite before merge.

This keeps individual ports isolated and makes it much easier to understand which patch introduced a regression.

---

## CI / GitHub Actions

GitHub Actions is used as the Continuous Integration layer for this fork.

During normal development, a draft PR receives:

- Django startup/system checks;
- targeted tests for changed or related modules;
- Ruff lint checks on changed Python lines.

Superseded runs are automatically cancelled when a newer commit is pushed to the same PR.

When a PR is marked ready for review, the complete Django regression suite is split into multiple parallel shards instead of running as one long monolithic job. Full coverage runs are kept for integration/release branch validation rather than slowing every development commit.

The purpose of CI here is not just code style. It is our automated protection against one source port silently breaking another.

---

## Source-port status

### Integrated into `chris/integration`

These customisations have already been moved from runtime patching into source-native implementations:

| Feature | Ported version / status |
| --- | --- |
| Quick Rating Overlay | v4.1.3 |
| Rapid Rating | v2.0.0 |
| Kodi Library Awareness | v1.0.3 |
| Derived TV Ratings | v1.1.0 |
| Derived Music Ratings | v1.0.0 |
| Smart Watched Dates | v1.0.3 |
| Music History cache-on-commit | integrated |
| Music History current-day relations fix | integrated |
| Kodi JSON-RPC / foundation work | integrated |
| Patch regression coverage for the above | integrated |

The old runtime patch harness is useful as historical evidence while porting, but the objective is to remove the need for runtime patch injection altogether.

### Currently being ported / reviewed

#### Kodi Control + Sync v2.7

Active PR: **#5**

This is one of the larger ports because it touches playback, watched-state processing, ratings, durable resume state, Kodi JSON-RPC control and shared Now Playing state.

The source-native port preserves the accepted behaviour while adapting it to current Floppy architecture, including:

- `start`, `pause`, `resume`, `seek`, `interval`, `stop` and `end` events;
- cache-only interval heartbeats so Kodi does not write playback progress to SQLite every few seconds;
- durable progress checkpoints on pause, seek, stop and end;
- deferred exact-item resume after TMDb Helper resolves the actual stream;
- authenticated pause/resume/stop and ±30 second seek controls;
- watched completion at the accepted Kodi threshold;
- protection against partial replays reopening completed seasons;
- movie and episode rating handling isolated from playback/history processing;
- episode rating writes that avoid `Episode.save()` watch-state side effects;
- Kodi-owned playback provenance so Kodi controls/reconciliation can never act on Plex or Jellyfin sessions;
- conservative cold/stale Now Playing reconciliation;
- source-native **Play in Kodi** actions for movies, shows, seasons and episodes.

The port deliberately reuses current Floppy's shared playback/progress and provider-resolution systems rather than restoring obsolete r13 copies of them.

#### Post-Watch Workflow v1.1.0

A separate source-port PR exists for the Post-Watch workflow. Its regression work includes repeated-play deduplication, dropped-episode handling, safe nullable episode handling, legacy dismissal-key migration and backfilling older movie watch records where necessary.

It remains separate from Kodi so the two substantial behavioural changes can be reviewed and validated independently.

---

## Remaining r13 work

The r13 baseline contains additional accepted customisations still to be reconciled with current source. The rough dependency order is intentional because several later features depend on earlier ones.

### Local patchset parity

After Kodi Control + Sync, remaining pieces from the aggregate local patchset will be reviewed individually. These include older Explore/IMDb/duplicate-aggregation behaviour where it is still missing from current Floppy.

### Integration Health Centre

The original Integration Health Centre provided read-only diagnostics for services such as Kodi, MDBList, TMDb, Redis, Celery and the database.

Its source port will be adapted to the new architecture. In particular, the old runtime-patch-harness health check does not make sense once these features live natively in source and must be replaced rather than copied.

### Rating Intelligence

The accepted Rating Intelligence work is split into core and advanced layers. It depends on the local-patchset/health work, so it comes later rather than being copied ahead of its dependencies.

### OAuth / integration security

The r13 patch set also contains the staged OAuth/scoped-token work:

- OAuth client registry;
- connected applications;
- device authorization;
- token exchange;
- metadata/revocation;
- scoped integration tokens.

These will be ported as source-native security/integration features after their dependencies are stable.

### Other accepted r13 work

The remaining baseline also includes features such as:

- Umbrella API performance work;
- additional integration/API changes;
- remaining aggregate local-patchset behaviour.

Each will receive the same source-review and regression process instead of wholesale file replacement.

---

## Why not just keep the patches?

Runtime patches got us working features quickly, but they create long-term problems:

- a patched file can silently overwrite newer upstream code;
- two patches can both modify the same function and undo each other;
- updating Floppy becomes risky because the runtime version and patched version diverge;
- debugging requires knowing both the source tree and the overlay tree;
- a clean reinstall requires reconstructing the entire patch environment;
- upstream tests do not naturally test the final patched runtime.

Moving the features into source gives us normal Git history, reviewable diffs, automated tests and a clean deployment path.

---

## Design rule: current source wins when behaviour has evolved

The r13 baseline is our **known-good behavioural reference**, not a command to reproduce old code literally.

A good example is episode ratings. The old Kodi patch targeted the latest watch row. Current Floppy has since formalised two distinct behaviours:

- a normal episode/detail rating belongs to the episode and is propagated across its replay rows;
- a History quick-rating is intentionally attached to one specific viewing.

The source port therefore keeps current Floppy's newer model while preserving the important historical Kodi safeguard: rating updates bypass `Episode.save()` so they cannot accidentally alter watched/season state.

That is the general rule for this project: preserve the intended user behaviour and historical bug fixes, but merge them into the architecture that exists **now**.

---

## Development rules

For this fork:

- do not develop directly on `chris/integration`;
- one logical patch or infrastructure change per feature branch/PR;
- keep implementation PRs draft while actively changing them;
- use the fast CI loop during development;
- run the full regression suite before merge;
- do not replace current source files with old r13 copies unless the current file has been explicitly reviewed first;
- preserve existing source-native ports when touching shared code;
- document deliberate departures from r13 behaviour;
- add regression tests for every important historical bug we recover.

Python quality checks follow the repository's existing tooling, including Ruff and the Django test suite.

---

## Repository layout relevant to this work

Most source-native ports live in the normal Floppy tree under `src/`.

Port-specific behavioural notes are kept under:

```text
docs/local-patches/
```

Those documents record the accepted behavioural contract and deliberate source-era adaptations for larger ports. They are intended to make later maintenance possible without having to reconstruct the reasoning from old patch archives or chat history.

---

## Upstream Floppy

This project remains based on Floppy and owes the vast majority of its code and product design to the upstream project maintained by Danny V Films and its contributors.

For normal Floppy information, use the upstream resources:

- **Repository:** https://github.com/dannyvfilms/Floppy
- **Releases:** https://github.com/dannyvfilms/Floppy/releases
- **Wiki:** https://github.com/dannyvfilms/Floppy/wiki
- **Container:** `ghcr.io/dannyvfilms/floppy`

This fork exists to maintain and source-integrate our custom Floppy behaviour while staying as compatible as practical with the evolving upstream codebase.

---

## Current objective

The immediate objective is simple:

> **Finish converting the accepted r13 runtime patch stack into a clean, tested, source-native Floppy build with no patch-overlay dependency.**

Once that is complete, `chris/integration` becomes the reproducible source of truth for our customised Floppy installation rather than `/opt/floppy/patches/` and a collection of container overlays.
