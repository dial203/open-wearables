# Fork decisions

Why this fork diverges from [`the-momentum/open-wearables`](https://github.com/the-momentum/open-wearables),
one entry per intentional divergence. Append-only: supersede an entry with a
later one rather than rewriting it, so the reasoning at the time stays readable.

[DIVERGENCE.md](./DIVERGENCE.md) is the generated file-by-file view; this file
is the *why*. When a sync conflicts with one of these, the entry says whether
to keep ours, take theirs, or reconcile.

**Add an entry whenever you change a file that also exists upstream.** A
fork-only file needs an entry only if it changes how shared code behaves.

Template:

```
## <short title>
- **Area**: backend / frontend / mcp / docs / tooling
- **Status**: active | superseded by <entry> | upstreamable | merged upstream
- **On conflict**: keep ours | take theirs | reconcile (how)
- **Why**:
```

---

> The entries below were reconstructed from commit history when this file was
> created, so they record *what* diverges and why, but not always the full
> discussion. Entries added from here on should be written at the time of the change.

## Fork tracking: generated report vs the upstream-sync skill

- **Area**: tooling, docs
- **Status**: active
- **On conflict**: keep both; they are different halves
- **Why**: `.ai/skills/upstream-sync/SKILL.md` (added on main) is the *procedure* plus a
  hand-written inventory of fork-local feature areas and the kind of upstream change that
  breaks each. `make fork-diff` generates `docs/fork/DIVERGENCE.md`, the mechanical
  per-file view. Neither replaces the other: the inventory explains *how* an area breaks
  and cannot be derived; the generated report catches a file that has quietly started
  diverging and was never added to the inventory. The skill's Compare step now points at
  `make fork-setup` / `fork-diff` so there is one remote setup rather than two, and the
  `upstream-sync/<date>` tags give the skill's verification step a base to diff against.
  Both were written against the same 0.9 sync, independently.

## Device registry: device / device_identity / device_history / device_link_proposal

- **Area**: backend, frontend
- **Status**: active
- **On conflict**: keep ours; re-apply upstream's change to `data_source` on top
- **Why**: Upstream models a data source as roughly one-per-provider, which cannot
  express one physical unit. Garmin reports `deviceName` only on activities; Apple and
  Google Health are aggregators; and the same unit arriving by several routes shares
  no identifier between them. The fork adds a registry above `data_source`
  (`app/models/device*.py`, `app/repositories/device_repository.py`,
  `app/services/devices/`, `app/api/routes/v1/devices.py`, migration `d3f1a8c2e5b4`)
  with hand editing, merge/split and an append-only audit trail. Documented in
  `docs/dev-guides/device-registry.mdx`.
  `data_source` itself changes only by gaining a nullable `device_id` FK, so an
  upstream change to that table should merge cleanly.
- **Upstreamable?** Probably not as a whole — it is opinionated toward research use,
  where knowing *which unit* produced a sample is the point. The per-route identity
  model could be.

## Relayed streams are keyed on the writing app, not the phone that relayed them

- **Area**: backend, frontend
- **Status**: active
- **On conflict**: keep ours; it only adds columns and narrows a grouping rule
- **Why**: A third-party app writing into HealthKit or Health Connect generally passes
  no device record, so the platform reports the phone that ran the app. Every app on one
  handset then reports the same model string, and the registry's within-route model
  grouping pooled all of them into a single device named after the phone — the
  over-merge the whole package exists to prevent, reached through the rule meant to
  prevent it. Renaming that device renamed every brand behind it, which made the
  registry unusable for relayed data.
  `relaying_host_model` (`app/services/devices/identity.py`) recognises that case
  narrowly — aggregator route, third-party writer, model inferring to `phone` — and
  detection then groups those sources on the writer's claim, keeps the handset in the
  new `device.host_model_raw` column as provenance, and leaves `model_raw` NULL because
  the provider never reported this unit's hardware. Over-splitting is the safe direction,
  so the doubt resolves toward firing.
  Everything a person then needs to identify the unit is editable beside the provider's
  claim rather than on top of it: `brand_display` joins `model_display`, plus `serial`
  and `firmware_version` (migration `c9d4e1f7a3b8`). `serial` is deliberately not an
  identity claim and groups nothing. `SourceMetadata` gained `device_display_name` and
  now takes the registry's `device_type` where it knows one, so the compare view names
  the unit rather than the handset. Existing rows are corrected by
  `scripts/data_migrations/split_host_relayed_devices.py`.
  `DeviceType` gained `eeg` and `headband` (migration `b7c2d9e4f1a6`); handset model
  codes (Pixel, Galaxy S, SM-S/G, LM-) now infer as `phone`, which is what makes the
  Android relay case detectable at all.
- **Upstreamable?** The host-relay rule and the handset model patterns are a plain bug
  fix and would be. The editable display fields ride on the registry, which is not.

## Device attribution helpers (pre-registry)

- **Area**: backend, frontend
- **Status**: active
- **On conflict**: keep ours, then re-apply upstream's change on top
- **Why**: The fork needs per-device attribution accurate enough for device
  validation work — knowing *which unit* produced a sample, not just which
  provider. Upstream treats a data source as roughly one-per-provider. Ours adds
  `app/utils/device_registry.py` (canonical brand, `ingestion_route`, display-name
  humanization), a `chest_strap` device type, `user_connection.device_label`, the
  brand backfill migration, and the `device` / `device_identity` /
  `device_attribution_history` tables with their management API and UI.
  Touches `data_source_repository.py`, `schemas/enums/device_type.py`,
  `schemas/utils/metadata.py` and the provider ingest paths.
- **Upstreamable?** Partly. `device_registry` and the device-type inference are
  generally useful and could go upstream; the full registry is opinionated toward
  research use and probably should not.

## `filter_by_priority` default differs between endpoints

- **Area**: backend
- **Status**: active — unresolved, deliberately
- **On conflict**: reconcile, do not silently pick one
- **Why**: The fork's summary endpoints (`api/routes/v1/summaries.py`) default
  `filter_by_priority=True`; upstream adopted the same parameter for
  `/timeseries` but defaults it to `False` (`api/routes/v1/timeseries.py:32`).
  The 2026-09-15 sync kept both defaults as-is to stay behaviour-preserving.
  Which way to reconcile is a product decision: `True` returns OW's prioritized
  winner, `False` returns every source. See `docs/dev-guides/consuming-all-data.md`.

## OW is a complete store, not the reconciler

- **Area**: docs
- **Status**: active
- **On conflict**: keep ours
- **Why**: `docs/dev-guides/consuming-all-data.md` is fork-only. Downstream apps
  must be able to pull *every* source's version of a metric, not just the winner,
  because method comparison needs both arms. It documents the attribution
  contract that downstream consumers and the MCP server depend on.

## Polar RR interval import

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: `app/services/polar_rr_import_service.py` is fork-only. Beat-to-beat
  RR intervals are needed for HRV analysis and are not part of upstream's Polar
  integration.

## FIT file retention

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: `workout_details.fit_file_key` keeps the original FIT file alongside the
  parsed workout, so a parsing change can be re-run against the raw recording
  rather than losing fidelity permanently.

## Apple XML source identity

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: Apple Health XML exports attribute sleep to the exporting device rather
  than the recording one. The fork resolves source identity per record so a watch's
  sleep is not credited to the phone that synced it.

## CI: local AI review and fork-specific build

- **Area**: tooling
- **Status**: active
- **On conflict**: keep ours
- **Why**: `.github/workflows/pr-review.yml` and `build.yml` plus
  `docker-compose.prod.yml` are fork-only infrastructure. The AI review step is
  deliberately non-blocking — it must never red a PR on timeout.

## A relayed stream groups on the writer *and* the host, not the writer alone

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: `relaying_host_model()` already recognises that an aggregator's
  `device_model` names the phone that ran the writing app, not the recorder. Keying
  those sources on the writer alone fixes the pooling but introduces the opposite
  error: one app's streams then group across every handset it ever synced through, so
  an Oura app relaying from a 2017 phone and a 2024 phone reads as one ring — two
  units merged, with no symptom, for anyone who replaced the ring in between. The key
  is therefore the pair (`DeviceIdentityKind.AGGREGATOR_WRITER_MODEL`), which
  over-splits instead; a person merges what is really one unit, and nothing was
  pooled while they decided. A claim on the bare host string is never stored — every
  app on that phone would make the same one.

## An unrecognised HealthKit writer keeps its own name

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: `resolve_brand()` falls back to the platform when no brand table matches,
  and that fallback used to overwrite the caller's `original_source_name`
  unconditionally in `ensure_data_source`. Every third-party HealthKit writer the
  tables had never heard of — Muse, AutoSleep, Eight Sleep, Hume — was therefore
  filed as "Apple", losing the only field that named the recorder and making the
  relay read as first-party Apple data. A recognised brand still wins over the
  caller's value; a readable name now survives when nothing matched.

## Deliberate detachment is a state, not an absence

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: `data_source.attribution_locked_at` distinguishes "a person unlinked this"
  from "never attributed". Without it, detection re-attached an unlinked source on the
  next sync and the unlink button looked broken. Fork-only column; an upstream merge
  that rewrites `data_source` must carry it.
## Fork version in the sidebar footer

- **Area**: frontend, tooling
- **Status**: active
- **On conflict**: reconcile — keep upstream's `__APP_VERSION__` wiring, re-apply the
  `__FORK_*` defines and `VersionFooter` on top
- **Why**: the footer used to show one version, which on a fork is ambiguous: `v0.9.0`
  is upstream's release, and says nothing about which fork build is running. It now
  shows both, each with the date its code last moved, so a bug report from a running
  instance identifies the exact tree. Upstream's date is the merge base with
  `upstream/main` rather than a release date: two syncs a month apart are both
  "v0.9.0", and the question being asked is how old the upstream code in this build
  is. Upstream's version still comes from
  `frontend/package.json` (synced, so never edited here); the fork's own version lives
  in fork-only `frontend/fork-version.json` to keep it out of every upstream merge.
  The frontend image is built from a `./frontend` context with `.git` excluded, so the
  build cannot read git — hence the committed stamp, refreshed by `make fork-stamp`
  (which `make build` runs). A host build or `pnpm dev` still prefers live git over the
  stamp, so a stale stamp only ever affects a Docker image; `FORK_VERSION`,
  `FORK_COMMIT` and `FORK_UPDATED_AT` override both for CI.
