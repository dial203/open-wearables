# Porting the fork's dashboard to the SvelteKit portal

Upstream replaced its React dashboard with a SvelteKit portal (`8ce9aa99`, synced
2026-10-05). The portal now lives in `frontend/`, byte-for-byte upstream except where this
file says otherwise. The fork's React dashboard moved, unchanged, to `frontend-react/` and
is still what `docker-compose.prod.yml` and the nightly image build serve. See the
"Dashboard" entry in [DECISIONS.md](./DECISIONS.md) for why.

The end state is one dashboard: the portal, carrying the fork's screens. Each feature below
is ported, checked against the React version on the same data, and ticked. When every row
is ticked, the serve switch flips to `frontend/` and `frontend-react/` is deleted.

## How to preview the portal

```bash
docker compose -f docker-compose.prod.yml --profile portal up -d portal   # on :3001
```

It reads the same backend as the React dashboard, so both can run side by side.

## Already in the portal

- `eeg` and `headband` device types (labels and icons) - `src/lib/providers/devices.ts`.
  The portal's own test checks its type list against the API's, so it failed until these
  were added.

## To port, in order

The order is by how much day-to-day work depends on the screen, not by size. Sizes are
the React code's line counts, tests excluded.

| # | Feature | React source (`frontend-react/src/`) | Lands in (`frontend/src/`) | Size |
|---|---|---|---|---|
| 1 | Several accounts per provider; account chip, label, classification (incl. `reference`) | `components/user/connection-card.tsx`, `add-provider-account-dialog.tsx`, `common/account-chip.tsx`, `lib/utils/account.ts` | `lib/components/users/connections/` (ConnectionCard, ConnectionMenu, ConnectionHeader) | ~630 |
| 2 | Pairing a second account | `routes/users/$userId/pair.index.tsx`, `hooks/use-oauth-connect.ts` | `routes/users/[id]/pair/`, `lib/components/pairing/` | ~220 |
| 3 | Devices: list, timeline, linking a data source, creating a device while linking | `components/user/devices-section.tsx`, `device-timeline-dialog.tsx`, `link-data-source-dialog.tsx`, `source-activity.tsx`, `lib/api/services/device.service.ts`, `hooks/api/use-devices.ts` | new tab under `routes/(app)/users/[id]/devices/`, `lib/components/users/detail/UserTabs.svelte` | ~2,990 |
| 4 | Source attribution on every record (device badge, "via" account, relayed writer) | `components/common/data-source-info.tsx`, `device-badge.tsx`, `device-type.tsx`, `lib/utils/device.ts` | shared component under `lib/components/` used by the sleep/workouts/activity/body tabs | ~490 |
| 5 | Compare tab, with every source's hypnogram stacked on one time axis | `components/user/compare-section.tsx`, `hypnogram.tsx`, `lib/utils/sleep.ts`, `hooks/api/use-health.ts`, `lib/api/services/health.service.ts` | new tab `routes/(app)/users/[id]/compare/`; charts in `lib/components/charts/` | ~1,180 |
| 6 | Sleep tab additions: all-source overnight RHR and RMSSD | `components/user/sleep-section.tsx` | `routes/(app)/users/[id]/sleep/`, `lib/components/sleep/` | ~110 |
| 7 | FIT download on workouts; Polar RR import | `components/user/workout-section.tsx`, connection card import action | `lib/components/events/`, connection menu | ~30 + RR UI |
| 8 | Priorities: per-account rows and the duplicate-relay notice | `routes/_authenticated/settings/-priorities-tab.tsx`, `lib/api/services/priority.service.ts` | `routes/(app)/settings/priorities/`, `lib/components/settings/priorities/` | ~110 |
| 9 | Fork version and upstream sync date in the sidebar footer | `components/layout/version-footer.tsx`, `lib/utils/build-info.ts`, `fork-version.json` | `lib/components/layout/AppVersion.svelte` | ~100 |
| 10 | WHOOP integration walkthrough page and screenshots | `public/whoop-integration.html`, `public/whoop-screenshots/` | `static/` (plain HTML, moves as is) | - |

## Notes for whoever ports a row

- **Data loading moves server-side.** The React app fetched in the browser with TanStack
  Query (`hooks/api/*`). The portal loads in `+page.server.ts` and keeps the session in
  Redis, so a React hook usually becomes a `load` function plus a typed client call in
  `lib/server/`. Mutations become form actions.
- **Port the tests too.** The React tests next to each file (`*.test.ts(x)`) are the
  behaviour to keep: `compare-section.test.ts`, `hypnogram.test.tsx`,
  `data-source-info.test.tsx`, `device.test.ts`, `use-oauth-connect.test.ts`. The portal
  runs unit tests in vitest and component tests in vitest browser mode.
- **The API is unchanged.** Every endpoint these screens use exists on the backend today;
  nothing in this port needs a backend change.
- **Check against the React screen on the same user** before ticking a row. The two can run
  side by side (see above).
