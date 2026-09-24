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

## Several provider accounts per user

- **Area**: backend, frontend, docs
- **Status**: active
- **On conflict**: keep ours; re-apply upstream's change to `user_connection` or
  `data_source` on top, and check whether the upstream path resolves a connection
  from `(user_id, provider)` — if it does, it needs an account scope
- **Why**: Upstream enforces one account per provider per user with a `UNIQUE`
  index on `user_connection(user_id, provider)`. That is right for a consumer app
  and wrong for device-comparison work, where one participant wears two units of
  the same brand at once, each on its own account, and keeping the two streams
  apart is the entire point. The fork replaces that index with two partial unique
  indexes on the *external* account — `(user_id, provider, provider_user_id)` and
  `(user_id, provider, lower(account_email))`, active rows only — so the same
  login still cannot be linked twice, and adds `account_label` / `account_email`
  so every data set is traceable to the login it came from.

  `data_source`'s identity index gains `user_connection_id`. This is the
  load-bearing half: two accounts with one provider report identical
  `device_model` and `source`, so without it both units' samples pool into one
  `data_source` and can never be separated again. Connection-less rows are
  adopted only when the user holds a single account — a wrong split is visible
  and reversible, a wrong merge is not.

  `app/utils/connection_context.py` is fork-only and is what keeps this from
  being a rewrite of every provider: the provider layer resolves credentials from
  `(user_id, provider)` throughout, so instead of threading a connection id
  through every signature, callers that know which account they are acting for
  bind a `ContextVar` and `UserConnectionRepository` honours it. Any upstream
  change that adds a new `(user_id, provider)` lookup on a sync or webhook path
  needs the same treatment.

  Known limit, deliberate: Garmin's 30-day backfill chain keeps progress in Redis
  keyed by user, so backfills for a user's several Garmin accounts run
  sequentially rather than concurrently — a second request is re-queued, not
  dropped. Making it concurrent means re-keying the whole backfill state module
  and was judged not worth the risk against the value.

## Fork version: CI supplies the commit, the stamp supplies the rest

- **Area**: frontend, tooling
- **Status**: active; refines "Fork version in the sidebar footer"
- **On conflict**: keep ours
- **Why**: the first version of this leaned on committed `frontend/fork-version.json`
  for every Docker build, and deployed images went stale immediately — `build.yml`
  pushes an image on every merge to main, but the stamp only moves when someone runs
  `make fork-stamp`, so the footer sat at one commit while 36 more landed. The deploy
  path never runs `make build`, so the Makefile's stamp step never fired. CI now passes
  `FORK_COMMIT` and `FORK_UPDATED_AT` from the pushed commit, which is exact by
  construction and cannot drift. The stamp keeps only what a human changes — the fork
  version and the upstream sync point — so staleness there is a real signal rather
  than an artefact. The args sit below `pnpm install` in the Dockerfile: they change on
  every commit, and above it they would bust the dependency layer cache on each build.

## Orphan adoption is judged at the row's age, not the account count today

- **Area**: backend
- **Status**: active; refines "Several provider accounts per user"
- **On conflict**: keep ours
- **Why**: putting `user_connection_id` into the data-source identity made a
  connection-less row and an otherwise identical connected row two different
  sources. `_can_adopt_orphans` was meant to heal that by letting a connection
  claim rows written before connections were recorded, but it asked how many
  accounts the user holds *now* and refused at two or more. So linking a second
  account retroactively unclaimed every older row of the *first* one: the next
  sync created a fresh source beside each orphan and both kept being written.
  Nothing re-files the old row afterwards, so the split is permanent and the
  device reads as a doubled series to anything that pools by provider or device
  — which is exactly what a validation study does. Seen in the wild on a Venu X1:
  two sources, 5,037 and 5,033 samples, both clean 1 Hz, both the same run.

  The question is now asked of the row: how many accounts existed when it was
  written. One means that account is the only thing that could have produced it,
  however many have been linked since. Two or more still refuses, because a wrong
  merge pools two units' histories irreversibly while a wrong split does not.
  Rows older than every connection (XML import, then connect) keep the previous
  behaviour, and an empty read means the account is being created in the same
  uncommitted transaction and is therefore the only candidate.

  This fixes the split going forward. Rows already forked stay forked; merging
  them is a separate, opt-in repair, because it has to drop one side of every
  colliding second and that is not something a migration should do unasked.

## `/timeseries` reads a comma-joined `types` list, and says how long each sample's window is

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: FastAPI only understands the repeated `types=a&types=b` form. A client
  that joined the list (`types=a,b`, which the Sleep Validation Hub did) got a
  400 on every sync, reported as a failed timeseries pull while every night
  looked like one with no HRV series, so Oura's 5-minute sleep RMSSD, stored
  here all along, never reached the hub. The route now splits comma-joined
  values before the enum check. An unknown name in a joined list is still
  refused, the same as it is in the repeated form.

  Oura's sleep `hrv` and `heart_rate` arrays are 300-second windows, but a
  `/timeseries` row carried nothing to say so. A consumer re-windowing a chest
  strap onto the sample had to guess, and guessed Apple's ~60 s SDNN window.
  The Oura save path now stamps `provider_metadata.interval_seconds`, and raw
  `/timeseries` rows expose it as `interval_seconds`: null where no provider
  stated a window, never inferred from cadence. Existing rows pick it up on the
  next sleep sync, because the upsert fills `provider_metadata` when the stored
  value differs.

## Polar and Garmin 5-minute HRV reach `/timeseries` as windows

- **Area**: backend
- **Status**: active; extends the entry above
- **On conflict**: keep ours
- **Why**: a validation study reads each wearable's 5-minute RMSSD against a
  chest strap, window by window. Oura's reached `/timeseries` with its window
  stated. The other two makers whose APIs publish the series did not:
  - **Polar:** Nightly Recharge's `hrv_samples` (AccessLink v3, keyed "HH:MM",
    5-minute RMSSD) were parsed and discarded. They are now stored, each at its
    window start. The keys are a wall clock with no offset, so each night is
    placed on the same night's sleep record (`sleep_start_time` carries the
    offset). A night with no sleep record is skipped and logged rather than put
    on a guessed zone. The sync fetches sleep once and shares it between the two
    tasks. This mapping follows the published v3 field names and has not yet
    been checked against a live payload.
  - **Garmin:** `hrvValues` state their window from the payload's own offset
    spacing (300 s in every payload seen; a single value states none).
    `lastNightAvg` sits in the same series at the sleep start and is now
    flagged `is_daily_total`, so it is not read as the first window and is left
    out of bucketed reads, as Google's daily HRV already is.

## Google Health defaults to list, not reconcile

- **Area**: backend
- **Status**: active; follows from "OW is a complete store, not the reconciler"
- **On conflict**: keep ours
- **Why**: `dataPoints:reconcile` returns one stream merged across every source
  on the Google account, with no device attribution. That is what the Fitbit
  app shows, and it is the right default for step totals. For a study that
  compares devices it is fatal: a Fitbit Air, an Amazfit strap relayed through
  Health Connect and a ring on one account become a single series under no
  device, and nothing downstream can separate them again. List mode keeps each
  source's points, each tagged with its device.

  The fork therefore defaults `google_use_reconcile` to `False`, and
  `.env.example` does the same. Two things follow:
  - A deployment whose `.env` sets `GOOGLE_USE_RECONCILE=true` explicitly keeps
    that value. This change does not reach it, so check the `.env`.
  - Rows already written under reconcile stay under their merged,
    device-less source. The switch applies from the next sync onward.

## Sleep-onset latency and WASO derived from stage intervals

- **Area**: backend
- **Status**: active, upstreamable
- **On conflict**: keep ours; `derived_from_stages` is one additive field on
  `SleepSession` and one call in `EventRecordService.get_sleep_sessions`, so
  re-apply both on top of upstream's version of either file
- **Why**: Muse S reaches this fork only as an Apple Health relay, which carries a
  total awake figure and no WASO or latency (HealthKit has no latency type). The
  total differs from WASO by the latency, so it cannot stand in for it, and the
  sleep-validation study scoring devices against the headband needs both.
  `app/algorithms/sleep_onset.py` derives them from the stored intervals for any
  source that has them. Onset is the first sleep interval; WASO runs to the end
  of the session, and wake after the final sleep interval is reported beside it.
  It is served under its own name rather than written into `stages`, because it
  is a figure this fork computed and not one the source stated.
  Deliberately separate from `SleepScoreService._parse_wearable_stages_for_interruptions`,
  which is upstream's and feeds the sleep score: that one ends WASO at the final
  awakening and treats `in_bed` and `unknown` intervals as sleep, so an Apple
  session's `in_bed` window puts onset at the session start. Changing it would
  change every sleep score; it is left as upstream wrote it.
