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
