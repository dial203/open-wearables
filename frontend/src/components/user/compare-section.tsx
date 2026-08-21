import { Fragment, useMemo, useState } from 'react';
import { format, addDays, subDays, startOfDay, endOfDay } from 'date-fns';
import { ChevronLeft, ChevronRight, Layers } from 'lucide-react';
import {
  useSleepSummaries,
  useRecoverySummaries,
} from '@/hooks/api/use-health';
import { DataSourceInfo } from '@/components/common/data-source-info';
import { SectionHeader } from '@/components/common/section-header';
import { formatMinutes, parseApiDate } from '@/lib/utils/format';
import type {
  RecoverySummary,
  SleepSummary,
  SourceMetadata,
} from '@/lib/api/types';

interface CompareSectionProps {
  userId: string;
}

/** A source column: one distinct provider/source/device that reported for the day. */
interface SourceColumn {
  key: string;
  source: SourceMetadata | null;
  provider: string;
  sleep?: SleepSummary;
  recovery?: RecoverySummary;
}

/** One comparison row: a metric read across every source column. */
interface MetricRow {
  label: string;
  group: string;
  /** Raw numeric value, used for the spread calculation. null = not reported. */
  getValue: (col: SourceColumn) => number | null;
  /** Display string for a cell. */
  format: (value: number | null) => string;
  /** Formats the max−min spread. Omit for metrics where a spread is meaningless. */
  formatSpread?: (spread: number) => string;
}

// `source` (the HealthKit / Health Connect writer) is part of the key: without it
// every brand relayed through Apple Health collapses into a single "apple" column,
// which is exactly the distinction this view exists to show.
const sourceKey = (source: SourceMetadata | null | undefined) =>
  `${source?.provider ?? 'unknown'}|${source?.source ?? ''}|${source?.device ?? ''}`;

const num = (v: number | null | undefined): number | null =>
  v === null || v === undefined ? null : v;

const round = (v: number | null) => (v === null ? '-' : String(Math.round(v)));

const METRIC_ROWS: MetricRow[] = [
  // ── Sleep ──
  {
    group: 'Sleep',
    label: 'Duration',
    getValue: (c) => num(c.sleep?.duration_minutes),
    format: (v) => formatMinutes(v),
    formatSpread: (s) => formatMinutes(s),
  },
  {
    group: 'Sleep',
    label: 'Time in bed',
    getValue: (c) => num(c.sleep?.time_in_bed_minutes),
    format: (v) => formatMinutes(v),
    formatSpread: (s) => formatMinutes(s),
  },
  {
    group: 'Sleep',
    label: 'Efficiency',
    getValue: (c) => num(c.sleep?.efficiency_percent),
    format: (v) => (v === null ? '-' : `${Math.round(v)}%`),
    formatSpread: (s) => `${Math.round(s)} pts`,
  },
  {
    group: 'Sleep',
    label: 'Deep',
    getValue: (c) => num(c.sleep?.stages?.deep_minutes),
    format: (v) => formatMinutes(v),
    formatSpread: (s) => formatMinutes(s),
  },
  {
    group: 'Sleep',
    label: 'REM',
    getValue: (c) => num(c.sleep?.stages?.rem_minutes),
    format: (v) => formatMinutes(v),
    formatSpread: (s) => formatMinutes(s),
  },
  {
    group: 'Sleep',
    label: 'Light',
    getValue: (c) => num(c.sleep?.stages?.light_minutes),
    format: (v) => formatMinutes(v),
    formatSpread: (s) => formatMinutes(s),
  },
  {
    group: 'Sleep',
    label: 'Awake',
    getValue: (c) => num(c.sleep?.stages?.awake_minutes),
    format: (v) => formatMinutes(v),
    formatSpread: (s) => formatMinutes(s),
  },
  {
    group: 'Sleep',
    label: 'Interruptions',
    getValue: (c) => num(c.sleep?.interruptions_count),
    format: round,
    formatSpread: (s) => String(Math.round(s)),
  },
  // ── Overnight vitals ──
  {
    group: 'Overnight vitals',
    label: 'Avg HR',
    getValue: (c) => num(c.sleep?.avg_heart_rate_bpm),
    format: (v) => (v === null ? '-' : `${Math.round(v)} bpm`),
    formatSpread: (s) => `${Math.round(s)} bpm`,
  },
  {
    group: 'Overnight vitals',
    label: 'Resting HR',
    // Only some providers file a recovery score (Oura reports readiness, Garmin
    // neither), so fall back to the resting_heart_rate series on the sleep summary.
    getValue: (c) =>
      num(c.recovery?.resting_heart_rate_bpm) ??
      num(c.sleep?.resting_heart_rate_bpm),
    format: (v) => (v === null ? '-' : `${Math.round(v)} bpm`),
    formatSpread: (s) => `${Math.round(s)} bpm`,
  },
  {
    group: 'Overnight vitals',
    label: 'HRV',
    // Oura, Garmin and Whoop all report RMSSD. Recovery used to return it in the field
    // named avg_hrv_sdnn_ms; upstream #1452 split the two, so recovery now carries a real
    // avg_hrv_rmssd_ms and leaves SDNN null. Prefer RMSSD from either summary and keep
    // SDNN as the last resort, so this reads correctly on both sides of that change.
    getValue: (c) =>
      num(c.recovery?.avg_hrv_rmssd_ms) ??
      num(c.recovery?.avg_hrv_sdnn_ms) ??
      num(c.sleep?.avg_hrv_rmssd_ms) ??
      num(c.sleep?.avg_hrv_sdnn_ms),
    format: (v) => (v === null ? '-' : `${Math.round(v)} ms`),
    formatSpread: (s) => `${Math.round(s)} ms`,
  },
  {
    group: 'Overnight vitals',
    label: 'Respiratory rate',
    getValue: (c) => num(c.sleep?.avg_respiratory_rate),
    format: (v) => (v === null ? '-' : `${v.toFixed(1)} br/min`),
    formatSpread: (s) => `${s.toFixed(1)}`,
  },
  {
    group: 'Overnight vitals',
    label: 'SpO₂',
    getValue: (c) =>
      num(c.recovery?.avg_spo2_percent) ?? num(c.sleep?.avg_spo2_percent),
    format: (v) => (v === null ? '-' : `${v.toFixed(1)}%`),
    formatSpread: (s) => `${s.toFixed(1)} pts`,
  },
  // ── Scores ──
  {
    group: 'Scores',
    label: 'Recovery score',
    getValue: (c) => num(c.recovery?.recovery_score),
    format: round,
    formatSpread: (s) => String(Math.round(s)),
  },
];

export function CompareSection({ userId }: CompareSectionProps) {
  // Default to last night (data for today is usually still incomplete).
  const [day, setDay] = useState<Date>(() =>
    subDays(startOfDay(new Date()), 1)
  );
  const dayKey = format(day, 'yyyy-MM-dd');

  // Pull a ±1 day window with all sources, then narrow to the selected day.
  const params = {
    start_date: startOfDay(subDays(day, 1)).toISOString(),
    end_date: endOfDay(addDays(day, 1)).toISOString(),
    limit: 100,
    filter_by_priority: false,
  };
  const { data: sleepData, isLoading: sleepLoading } = useSleepSummaries(
    userId,
    params
  );
  const { data: recoveryData, isLoading: recoveryLoading } =
    useRecoverySummaries(userId, params);

  const isLoading = sleepLoading || recoveryLoading;

  // Union of every source that reported sleep or recovery for this day.
  const columns = useMemo<SourceColumn[]>(() => {
    const byKey = new Map<string, SourceColumn>();

    const upsert = (source: SourceMetadata | null | undefined) => {
      const key = sourceKey(source);
      let col = byKey.get(key);
      if (!col) {
        col = {
          key,
          source: source ?? null,
          provider: source?.provider ?? 'unknown',
        };
        byKey.set(key, col);
      }
      return col;
    };

    for (const s of sleepData?.data ?? []) {
      if (format(parseApiDate(s.date), 'yyyy-MM-dd') !== dayKey) continue;
      const col = upsert(s.source);
      // Keep the longest session when a source reports several (naps + main sleep).
      if (
        !col.sleep ||
        (s.duration_minutes ?? 0) > (col.sleep.duration_minutes ?? 0)
      ) {
        col.sleep = s;
      }
    }
    for (const r of recoveryData?.data ?? []) {
      if (format(parseApiDate(r.date), 'yyyy-MM-dd') !== dayKey) continue;
      upsert(r.source).recovery = r;
    }

    return [...byKey.values()].sort((a, b) =>
      a.provider.localeCompare(b.provider)
    );
  }, [sleepData, recoveryData, dayKey]);

  // Only show rows at least one source reported, and group them.
  const groups = useMemo(() => {
    const populated = METRIC_ROWS.filter((row) =>
      columns.some((c) => row.getValue(c) !== null)
    );
    const out: { name: string; rows: MetricRow[] }[] = [];
    for (const row of populated) {
      const last = out[out.length - 1];
      if (last && last.name === row.group) last.rows.push(row);
      else out.push({ name: row.group, rows: [row] });
    }
    return out;
  }, [columns]);

  const hasData = columns.length > 0;
  const isToday = dayKey === format(new Date(), 'yyyy-MM-dd');

  return (
    <div className="rounded-2xl border border-border/60 bg-gradient-to-br from-card/80 to-card/40 backdrop-blur-xl overflow-hidden">
      <SectionHeader
        title="Compare sources"
        rightContent={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setDay((d) => subDays(d, 1))}
              className="p-1.5 rounded-md border border-border/60 text-muted-foreground hover:text-foreground hover:bg-card/60 transition-colors"
              aria-label="Previous day"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <input
              type="date"
              value={dayKey}
              max={format(new Date(), 'yyyy-MM-dd')}
              onChange={(e) => {
                if (e.target.value) setDay(parseApiDate(e.target.value));
              }}
              className="bg-transparent border border-border/60 rounded-md px-2 py-1 text-xs text-foreground"
              aria-label="Comparison date"
            />
            <button
              onClick={() => setDay((d) => addDays(d, 1))}
              disabled={isToday}
              className="p-1.5 rounded-md border border-border/60 text-muted-foreground hover:text-foreground hover:bg-card/60 transition-colors disabled:opacity-40 disabled:pointer-events-none"
              aria-label="Next day"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        }
      />

      <div className="p-6">
        <p className="text-xs text-muted-foreground mb-4">
          Every source that reported for{' '}
          <span className="text-foreground font-medium">
            {format(day, 'EEEE, MMM d yyyy')}
          </span>
          , side by side. Sleep is attributed to the morning you woke.{' '}
          <span className="whitespace-nowrap">Spread = max − min.</span>
        </p>

        {isLoading ? (
          <div className="space-y-2">
            {[1, 2, 3, 4, 5].map((i) => (
              <div
                key={i}
                className="h-9 bg-muted/40 rounded animate-pulse"
                style={{ animationDelay: `${i * 60}ms` }}
              />
            ))}
          </div>
        ) : !hasData ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <Layers className="h-6 w-6 text-muted-foreground/60" />
            <p className="text-sm text-muted-foreground">
              No sleep or recovery data from any source for this day
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-border/60">
                  <th className="text-left font-medium text-xs text-muted-foreground uppercase tracking-wider py-2 pr-4 sticky left-0 bg-card/80 backdrop-blur-sm">
                    Metric
                  </th>
                  {columns.map((col) => (
                    <th
                      key={col.key}
                      className="text-right font-medium py-2 px-3 min-w-[120px]"
                    >
                      <div className="flex flex-col items-end gap-1">
                        <DataSourceInfo source={col.source} />
                      </div>
                    </th>
                  ))}
                  {columns.length > 1 && (
                    <th className="text-right font-medium text-xs text-muted-foreground uppercase tracking-wider py-2 pl-3 min-w-[90px]">
                      Spread
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => (
                  <Fragment key={group.name}>
                    <tr>
                      <td
                        colSpan={columns.length + (columns.length > 1 ? 2 : 1)}
                        className="pt-4 pb-1 text-[10px] font-medium text-muted-foreground uppercase tracking-wider sticky left-0"
                      >
                        {group.name}
                      </td>
                    </tr>
                    {group.rows.map((row) => {
                      const values = columns.map((c) => row.getValue(c));
                      const present = values.filter(
                        (v): v is number => v !== null
                      );
                      const min = present.length ? Math.min(...present) : null;
                      const max = present.length ? Math.max(...present) : null;
                      const spread =
                        present.length > 1 && min !== null && max !== null
                          ? max - min
                          : null;

                      return (
                        <tr
                          key={`${group.name}-${row.label}`}
                          className="border-b border-border/40 last:border-0 hover:bg-card/40 transition-colors"
                        >
                          <td className="py-2 pr-4 text-muted-foreground sticky left-0 bg-card/80 backdrop-blur-sm">
                            {row.label}
                          </td>
                          {values.map((v, i) => {
                            // Flag the extremes only when they're actually different.
                            const isExtreme =
                              spread !== null && spread > 0 && v !== null;
                            const isMin = isExtreme && v === min;
                            const isMax = isExtreme && v === max;
                            return (
                              <td
                                key={columns[i].key}
                                className={`py-2 px-3 text-right tabular-nums ${
                                  v === null
                                    ? 'text-muted-foreground/40'
                                    : 'text-foreground'
                                }`}
                              >
                                <span
                                  className={
                                    isMax
                                      ? 'text-[hsl(var(--success-muted))]'
                                      : isMin
                                        ? 'text-sky-400'
                                        : ''
                                  }
                                >
                                  {row.format(v)}
                                </span>
                              </td>
                            );
                          })}
                          {columns.length > 1 && (
                            <td className="py-2 pl-3 text-right tabular-nums text-xs text-muted-foreground">
                              {spread === null || spread === 0
                                ? '—'
                                : (row.formatSpread?.(spread) ??
                                  String(Math.round(spread)))}
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
