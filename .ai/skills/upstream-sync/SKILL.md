---
name: upstream-sync
description: Merge the-momentum/open-wearables into this fork - compare, resolve, sweep for fork breakage, verify, commit. Use for any "sync upstream", "merge upstream", "pull in the new release" task.
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - Bash
---

# Upstream sync

This repo is a fork of `the-momentum/open-wearables` that carries research-specific
work (device/brand resolution, Polar RR import, FIT-file storage, Oura ring splitting,
priority-collapsed summaries). Upstream moves fast and renames things. The merge itself
is usually easy; **the damage is always in fork-only code that references upstream
symbols that moved.** Budget your attention accordingly.

Default posture: **accept upstream.** Keep a fork divergence only when it carries a
feature upstream does not have, or when adopting upstream would break a consumer
(frontend, MCP, analysis scripts). Never resolve a conflict by reverting an upstream
change you merely find surprising - check what it was for first (`git log -1 <sha>`).

## 1. Compare

```bash
make fork-setup   # adds the upstream remote and fetches it, idempotently
git log --oneline --no-merges HEAD..upstream/main --date=short --format='%h %ad %s'
git diff --stat HEAD...upstream/main | tail -40
```

`make fork-diff` regenerates `docs/fork/DIVERGENCE.md`: the last common commit, how far
ahead and behind we are, and every diverged file marked fork-only or modified. Use it
alongside the inventory below - the inventory says *which feature areas* break and how,
the generated report says *which files* currently differ, so a file that has quietly
started diverging cannot go unlisted. Run it again after the merge and commit the result,
then `make fork-tag` to anchor the sync for `git diff upstream-sync/<date>..HEAD`.

Read every commit subject before merging. Flag for the human, in the summary and the
commit message:

- **Renames of series types, provider slugs, or event names** - these are API-visible
  and can silently change what an analysis pipeline receives.
- **Defaults that flip** (a field that used to be returned now behind an `include=`,
  a filter default changing) - silent data loss for consumers, no error.
- **Reverts of features this fork already merged** - decide explicitly whether to
  re-apply; do not let it disappear unnoticed.
- **Data-migration scripts** under `backend/scripts/data_migrations/`. Check whether
  each is wired into `backend/scripts/start/app.sh` (automatic) or manual. Manual ones
  are an operator task on the deployment and must be surfaced, not buried.
- **New alembic revisions** - if upstream added one, the fork needs a merge revision
  (see §5).

## 2. Merge

```bash
git merge --no-commit --no-ff upstream/main
git status --short | grep -E '^(UU|AA|DU|UD|AU|UA|DD)'
```

Most conflicts are additive: a fork feature sits on the line upstream rewrote. Keep
both sides. The resolved file should read as if the fork feature had been written on
top of upstream's version, not bolted beside it.

## 3. Sweep for fork breakage (the part that matters)

A clean merge proves nothing. Upstream renames symbols and moves modules; fork-only
files referencing them still compile as text and fail at import or at test time.

```bash
# Enum/symbol renames: read what upstream did to the enums, then grep fork code for the old names
git diff HEAD@{1}...upstream/main -- backend/app/schemas/enums/ | grep '^[-+]' | head -40

# Module moves break @patch("dotted.path") targets - strings, so ruff and ty never see them
cd backend && uv run python scripts/check_patch_targets.py
```

Then import the app - the fastest single check that nothing fork-only references a
deleted symbol:

```bash
cd backend && SECRET_KEY=x MASTER_KEY=dGVzdC1tYXN0ZXIta2V5LWZvci10ZXN0aW5nLW9ubHk= \
  uv run python -c "import app.main; print('import ok')"
```

### Fork divergence inventory

Check each area that upstream's diff touches. These are the fork's own code - upstream
will never fix them for you.

| Area | Fork files | Breaks when upstream... |
|---|---|---|
| Brand + ingestion route | `backend/app/utils/device_registry.py`, `tests/utils_tests/test_device_registry.py`, `test_ingestion_route.py`, migration `b7e3c1a9d2f4` | renames or splits a `ProviderName` member (this broke on the 0.9 `google` -> `google_health`/`health_connect` split) |
| Priority-collapsed summaries | `backend/app/api/routes/v1/summaries.py` (`filter_by_priority=True`), `summaries_service.py`, `tests/services/test_summaries_service.py`, `tests/api/test_summary_priority_defaults.py` | changes paging params or the summary signatures. Default stays `True` here and `False` on `/timeseries` - deliberate, see the pin test |
| FIT-file storage | `fit_files_dir` in `app/config.py`, `has_fit_file` in `schemas/responses/activity/events.py` + `event_record_service.py`, `raw_payload_storage.py`, migration `a1f4c7e9d3b2`, `tests/services/test_fit_file_storage.py` | adds fields to `Workout` or reworks raw storage |
| Polar RR intervals | `backend/app/services/polar_rr_import_service.py`, `tests/services/test_polar_rr_import.py` | reworks Polar ingestion |
| Oura ring split | `scripts/data_migrations/split_oura_sources_by_ring_setup.py`, `tests/providers/oura/test_ring_configuration.py`, `providers/oura/data_247.py` | changes Oura source naming |
| Device type / labels | `schemas/enums/device_type.py` (chest strap), `data_source_repository.py`, migrations `c4e8f1a2b9d7`, `e2b7a4c1f8d3`, `reclassify_data_source_device_type.py` | changes data-source creation or device inference |
| Apple/SDK sleep | `app/services/sdk/sleep_service.py` (per-device session scoping, HealthKit stage vocabulary), `constants/series_types/sdk/sleep_types.py`, `tests/services/test_sleep_service.py`, `tests/constants/test_sleep_stage_mapping.py` | moves the SDK namespace (0.9 moved it out of `apple/`) |
| Frontend | `components/user/compare-section.tsx`, `common/device-badge.tsx`, `lib/utils/device.ts`, `components/user/sleep-section.tsx` | reworks source badges or summary hooks |
| MCP tools | `mcp/app/tools/*` (full-pagination walk, `get_menstrual_cycles`), `mcp/tests/test_tools.py` | changes summary pagination or response shape |
| Several accounts per provider | `app/models/user_connection.py` (partial unique indexes, `account_label`/`account_email`), `app/models/data_source.py` (identity includes `user_connection_id`), `app/utils/connection_context.py`, `user_connection_repository.py`, `data_source_repository.py`, `api/routes/v1/connections.py` (`/accounts/{id}`), `templates/base_oauth.py` (`_resolve_target_connection`), migration `b5d41c7a9e02`, `tests/repositories/test_multi_account_connections.py`, `test_data_source_per_account.py`, `tests/providers/test_oauth_account_resolution.py`, `tests/api/v1/test_connection_accounts.py`, `tests/tasks/test_sync_vendor_data_accounts.py` | re-adds `UNIQUE (user_id, provider)` on `user_connection`, changes `uq_data_source_identity`, or adds a sync/webhook path that resolves a connection from `(user_id, provider)` - that path needs an `active_connection` scope or it will act on the oldest account |
| Fork infra | `.github/workflows/build.yml` (nightly images), `.github/workflows/pr-review.yml` (local Ollama review on self-hosted runners), `docker-compose.prod.yml`, `docs/dev-guides/consuming-all-data.md` | - (fork-only, upstream never touches these) |

**Consumers that assume a default.** The MCP tools are written around one summary
record per day, and the frontend passes `filter_by_priority: false` only where it wants
per-source rows. Any upstream change to a default must be checked against both before
adopting it.

**Declined divergences** (revisit only if the human asks):

- MCP HTTP transport + OAuth, reverted upstream in #1640. Restore with
  `git revert 126735d9`, keep the current `README.md` on the one conflict, re-add the
  local-or-remote bullet to its MCP section. ~626 lines, `mcp/app/oauth.py` has no
  test coverage, and it re-conflicts on every sync that touches `mcp/app/main.py`.

## 4. Verify

No Docker in the sync sandbox, so bring up Postgres and Redis directly:

```bash
PGBIN=$(ls -d /usr/lib/postgresql/*/bin | tail -1); PGDATA=/tmp/pgdata
rm -rf $PGDATA && mkdir -p $PGDATA && chown postgres:postgres $PGDATA
su postgres -c "$PGBIN/initdb -D $PGDATA -U open-wearables --auth=trust"
su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l /tmp/pg.log -o '-p 5432 -k /tmp' start"
$PGBIN/createdb -h 127.0.0.1 -U open-wearables open_wearables_test
redis-server --port 6379 --daemonize yes --dir /tmp

export ENV=test SECRET_KEY=test-secret-key-for-ci \
  MASTER_KEY=dGVzdC1tYXN0ZXIta2V5LWZvci10ZXN0aW5nLW9ubHk= \
  TEST_DATABASE_URL="postgresql+psycopg://open-wearables@127.0.0.1:5432/open_wearables_test" \
  TEST_REDIS_URL="redis://localhost:6379/0"
```

All of it has to pass before the merge is committed - this mirrors `.github/workflows/ci.yml`:

```bash
cd backend && uv run pytest -q && uv sync --group code-quality \
  && uv run ruff check && uv run ruff format --check && uv run ty check \
  && uv run python scripts/check_migrations.py --base origin/main
cd ../mcp && uv run pytest -q && uv run ruff check && uv run ruff format --check && uv run ty check
cd ../frontend && pnpm install --frozen-lockfile && pnpm run test && pnpm run lint \
  && pnpm run format:check && pnpm run build
```

Report real counts. A merge that was not verified is not a merge that is done.

## 5. Migrations

`AGENTS.md` has the rule; the fork-specific part is that each sync that brings an
upstream revision needs a **merge revision** whose `down_revision` is a tuple of both
heads (see `2026_09_14_1522-923694d3b177_merge_upstream_0_8_into_fork.py`). Never edit a
migration already on `main`. `uv run alembic heads` must print exactly one head, and
`check_migrations.py` must pass.

## 6. Commit

One merge commit, body written for the person who reads it in six months:

- what came in (commit count, the upstream tag, the notable PR numbers)
- alembic head after the merge, and any data-migration scripts - automatic vs manual
- each conflict, the file, and why it resolved the way it did
- each fork breakage found in §3 and how it was fixed
- API-visible behaviour changes a consumer would notice
- the verification numbers from §4

Push to the working branch. Do not open a PR unless asked.
