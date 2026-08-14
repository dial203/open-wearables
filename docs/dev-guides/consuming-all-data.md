# Consuming Open Wearables data: source attribution & pulling everything

> Reference for **downstream apps** that read from an Open Wearables (OW) instance.
> Drop this into your app repo / LLM context so the app knows how OW attributes
> data, why the same metric can appear more than once, and how to pull **all**
> sources instead of only OW's prioritized "winner."

## The one thing to know

**OW is a faithful, complete store. It is not the reconciler.**

OW ingests everything each provider sends and keeps every source's version of the
data. It *can* collapse a day down to a single "best" source for convenience, but
that collapse is **opt-out** on the endpoints that support it. If your app does its
own reconciliation (dedup, trust ranking, merging), you want the **raw, all-sources**
view — see [Pulling ALL sources](#pulling-all-sources).

---

## How a reading is attributed

Every data point, event, and summary in OW is tied to a **`DataSource`** — the
identity of *where the reading came from*. The columns that matter:

| Field | Meaning | Example |
|---|---|---|
| `provider` | **Ingestion path** — the API/integration the data arrived through. | `oura`, `google`, `garmin`, `whoop`, `apple` |
| `original_source_name` | **Canonical brand** the data actually came from, resolved regardless of path. | `Oura`, `Garmin`, `Whoop` |
| `device_model` | Raw hardware string as reported by the provider (may be null). | `Oura Ring Gen3 Horizon`, `Forerunner 965`, `Watch6,2` |
| `device_type` | Coarse hardware class. | `watch`, `ring`, `band`, `phone`, `scale`, `other` |
| `source` | Fine-grained sub-source tag, mostly for aggregator paths (e.g. Apple HealthKit bundle IDs). | `com.oura.oura`, `com.whoop.android` |

**Source identity** (the uniqueness key) is:
`(user, provider, COALESCE(device_model,''), COALESCE(source,''))`.

### `provider` (path) ≠ `original_source_name` (brand)

This is the crux of duplication. The **same brand** can reach OW through **different
paths**, and each path is a distinct `DataSource`:

- **Direct:** you connect Oura → OW pulls from the Oura API. `provider=oura`, `original_source_name=Oura`.
- **Via Apple Health:** your phone writes Oura data into Apple HealthKit → OW ingests the Apple export. `provider=apple`, `source=com.oura.oura`, `original_source_name=Oura`.
- **Via Google Health:** same idea through Google. `provider=google`, `original_source_name` resolved to the real brand.

So "Oura sleep" for one night can legitimately appear **2–3 times** in OW — once per
path — with the *same* `original_source_name` but *different* `provider`. That's not
a bug; it's OW faithfully recording that the brand delivered the same night through
multiple pipes. **Your app decides** which copy to trust; OW just keeps them all.

### Device identification by provider

| Provider | Device auto-captured? | Notes |
|---|---|---|
| Garmin | ✅ | From the activity/summary payload. |
| Google Health | ✅ | Device metadata in payload where present. |
| Polar | ✅ | From payload. |
| Strava | ✅ | Device name on activities. |
| Oura | ✅ (wired via `ring_configuration`) | Model derived, e.g. `Oura Ring Gen3 Horizon`. |
| Whoop | ❌ | Whoop does not report a device model → **manually entered** (see below). |

When a provider doesn't report a device, an operator can set one on the OW side:
`PUT /users/{user_id}/connections/{provider}/device-label`. That label backfills
`device_model` on that connection's sources (including previously null rows), so the
attribution becomes complete after the fact.

---

## The priority hierarchy (a convenience, not a filter you're stuck with)

OW keeps two ranking tables so it *can* answer "just give me the one best source per
day":

- **`provider_priority`** — rank between providers/paths.
- **`device_type_priority`** — default: `watch=1, band=2, ring=3, phone=4, scale=5, other=6` (lower wins).

When priority filtering is on, OW collapses each day to the single highest-priority
source. **This is purely for consumers that want a pre-reconciled single stream.** If
your app does its own reconciliation, ignore the ranking and pull everything.

---

## Pulling ALL sources

Here's the all-sources story per endpoint. **Bold** = what to pass to get everything.

### Daily summaries — `filter_by_priority=false`

```
GET /users/{user_id}/summaries/sleep?start_date=…&end_date=…&filter_by_priority=false
GET /users/{user_id}/summaries/recovery?…&filter_by_priority=false
GET /users/{user_id}/summaries/activity?…&filter_by_priority=false
```

- **Default is `true`** → one collapsed row per day (highest-priority source only).
- **Pass `filter_by_priority=false`** → **one row per `(date, source/device)`** — every
  source's summary for that day, each row carrying its own `source` (provider + device).

> ⚠️ This is the endpoint that used to *always* collapse. As of the
> `filter_by_priority` opt-out, summaries can now return every source. If your app
> was only ever seeing one source per day from `/summaries/*`, this is why — add
> `filter_by_priority=false`.

### Sleep sessions — already all-sources by default

```
GET /users/{user_id}/events/sleep?start_date=…&end_date=…
```

- **Default is `filter_by_priority=false`** → all sources' sessions. No action needed.
- Pass `filter_by_priority=true` only if you *want* the collapsed single-source view.

### Time-series (raw biometrics/activity) — always all-sources

```
GET /users/{user_id}/timeseries?start_time=…&end_time=…&types=…&resolution=raw
```

- No priority filtering at all. Raw samples from **every** source come through.
  Use `resolution=raw` for full fidelity (or `1min|5min|15min|1hour` to downsample).

### Workouts — all-sources

```
GET /users/{user_id}/events/workouts?start_date=…&end_date=…
```

- No priority collapse; every provider's workouts are returned.

### Enumerate a user's sources

```
GET /users/{user_id}/data-sources
```

Returns `{ "items": [...], "total": N }`. Each item:

| Field | Notes |
|---|---|
| `id` | **Stable DataSource id — key on this.** Survives re-ingestion; immune to brand/casing noise. |
| `provider` | Ingestion path (`apple`, `google`, `garmin`, …) |
| `device_model` | Raw hardware string, may be null |
| `device_type` | `chest_strap` \| `watch` \| `band` \| `ring` \| `phone` \| `scale` \| `other` \| `unknown` — always set |
| `source` | Sub-source tag (Apple HealthKit / Health Connect bundle id) |
| `original_source_name` | Canonical brand |
| `display_name` | Pre-formatted "Provider · Model" for operator UIs |
| `user_id`, `user_connection_id`, `software_version` | |

**Prefer `id` over a composite key.** The DataSource uniqueness key is
`(user_id, provider, COALESCE(device_model,''), COALESCE(source,''))` —
`original_source_name` is *not* part of it, so brand casing (`FITBIT` vs `Fitbit`)
never splits a source, and two rows showing the same brand are genuinely distinct
sources differing by `device_model` and/or `source`.

### Direct from the maker, or relayed?

`ingestion_route` says whether a source came straight from the manufacturer's API or
was relayed through an aggregator platform — the distinction that `provider` alone
forces you to hardcode:

| `ingestion_route` | Meaning | Example |
|---|---|---|
| `direct` | Straight from the maker's own API | `provider=oura`, brand `Oura` |
| `aggregator` | Relayed through a platform | `provider=apple`, brand `Oura` — Oura data via Apple Health |

Aggregator platforms are Apple Health, Google Health / Health Connect, Samsung Health
and Strava. When `ingestion_route` is `aggregator`, **`provider` names the platform**
and **`original_source_name` names the brand that actually recorded the data**.

A platform carrying its *own* brand stays `direct` — an Apple Watch inside Apple
Health is still first-party. The classification is deliberately conservative: a source
is only called `aggregator` when a brand other than the platform's own can be named,
so an unrecognised brand is never mislabelled as relayed.

The same field is on every sample's `source` object, so you can filter a time series
to maker-direct data without a second call.

**Use `device_type` to separate real wearables from phone/app relays** rather than
pattern-matching model strings. It is always populated (`unknown` rather than null when
it can't be inferred). Note that a wearable relayed through Apple/Google Health carries
the *conduit phone* in `device_model`, so such a source types as `phone` — the brand is
still in `original_source_name`.

### Joining time-series samples back to a source

Every `source` object (on `/timeseries`, workouts, and sleep sessions) carries the
identity needed to join back to `/data-sources`:

```jsonc
"source": {
  "provider": "apple",                // the ingestion path (DataSource.provider)
  "source": "com.oura.oura",          // the writer inside that path
  "device": "iPhone18,1",
  "device_name": "iPhone 17 Pro",     // marketing name, derived from `device`
  "device_type": "phone",
  "data_source_id": "…",              // == items[].id from /data-sources
  "ingestion_provider": "apple",      // alias of `provider`
  "source_tag": "com.oura.oura",      // alias of `source`
  "original_source_name": "Oura",
  "ingestion_route": "aggregator"     // Oura data relayed via Apple Health
}
```

Use **`data_source_id`** as the join key.

ℹ️ Before 0.7.0 the `provider` field carried the sub-source tag rather than the
ingestion provider, which is why `ingestion_provider` and `source_tag` exist. As of
0.7.0 `provider` and `source` hold those values directly; the two explicit fields
remain as stable aliases, so a consumer pinned to either name keeps working.

### Data inventory / counts

```
GET /users/{user_id}/summaries/data?start_date=…&end_date=…
```

- Per-user counts grouped by series type, event type, and provider. Good for a quick
  "what's actually in here and from whom" check.

---

### Raw FIT files — `/events/workouts/{workout_id}/fit`

When the instance has FIT retention enabled (`STORE_FIT_FILES=true`), OW keeps the raw
`.fit` file for workouts that deliver one (**Garmin only** — Strava/others don't expose a
raw FIT over their API) and serves it over HTTP:

```
GET /users/{user_id}/events/workouts/{workout_id}/fit
```

- Returns the `.fit` bytes as an attachment (`application/vnd.ant.fit`).
- **404** if the workout doesn't exist for the user, or no FIT file is stored for it.
- The workout list/detail responses carry a **`has_fit_file`** boolean — use it to know
  which workouts have a downloadable file instead of probing each one.

```
GET /users/{id}/events/workouts?...      → each workout has "has_fit_file": true|false
GET /users/{id}/events/workouts/{workout_id}/fit   → the raw .fit bytes
```

The `.fit` is the provider's original file (full per-record streams, laps, developer
fields) — parse it with any FIT library. OW also ingests the workout's per-second samples
into `/timeseries` when `INGEST_WORKOUT_SAMPLES=true`, so apps can choose the raw file or
the normalized series.

### Gold-standard RR intervals (Polar H10) — import + `rr_interval` series

Raw beat-to-beat **RR intervals** (e.g. from a Polar H10 chest strap) are *not* available
through Polar's AccessLink cloud API — they only exist in the **RR CSV** you export from
Polar Flow (`duration,offline` header, one interval in ms per row). OW ingests that file
and exposes it as a normal time series:

**Import** (used by an automated fetch job, not a browser):
```
POST /users/{user_id}/import/polar/rr?workout_id={workout_id}
  (multipart body: file=<the RR CSV>)
```
- Ties the RR data to an existing OW workout; per-beat timestamps are reconstructed from
  that workout's start time (the CSV has none).
- `offline`-flagged rows (strap dropouts) are excluded, but the clock still advances over
  them so the remaining beats stay correctly timed.
- 404 if the workout isn't found for the user; 400 if the CSV is malformed. Re-importing
  the same file is idempotent (upsert on timestamp).

**Read** — it's a first-class series, so pull it like any other:
```
GET /users/{user_id}/timeseries?types=rr_interval&resolution=raw&start_time=…&end_time=…
```
- Each sample's `value` is one RR interval in **milliseconds**, timestamped at the beat
  that closes it. Use `resolution=raw` — downsampling would average intervals and destroy
  the HRV signal.
- High volume: an hour of RR is ~4–5k samples (an overnight recording ~20k+), so query
  bounded windows.

## Rule of thumb for a downstream app

1. **Pull raw / all-sources** everywhere: `filter_by_priority=false` on `/summaries/*`,
   default on `/events/sleep`, raw on `/timeseries`.
2. **Group by `original_source_name`** (the real brand), not `provider` (the path) —
   otherwise the same Oura night looks like three different devices.
3. **Do your own reconciliation** using `provider` (path), `device_model`/`device_type`,
   and timestamps. OW's priority tables are only a suggested default — treat them as a
   hint, not ground truth.
4. Don't assume one row per day. With all-sources on, expect N rows per day and
   dedup/merge in your app.

---

## Auth

All read endpoints take the OW API key (`ApiKeyDep`). Send it the way your OW instance
is configured (API key header). See the OW API reference for the exact header name.
