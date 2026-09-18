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

## Aggregator routes group on the writer, not the relaying handset

- **Area**: backend
- **Status**: active
- **On conflict**: keep ours
- **Why**: on Apple Health, Health Connect, Google Health and Samsung Health,
  `device_model` is the phone that synced the batch (HealthKit reports
  `productType`), so grouping devices on it pooled every relaying app behind one
  handset into a single device — a silent over-merge that only surfaces once the
  samples are already mixed in an analysis. These routes key on a writer/model pair
  instead (`DeviceIdentityKind.AGGREGATOR_WRITER_MODEL`). Touches
  `app/services/devices/identity.py` and `detection.py`, both fork-only today; if
  upstream grows a device registry, the pairing rule is the part to keep.

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
