import { useMemo, useState, type ReactNode } from 'react';
import { format } from 'date-fns';
import type { SleepStage } from '@/lib/api/types';
import {
  HYPNOGRAM_STAGE_ORDER,
  SLEEP_STAGE_HEX,
  SLEEP_STAGE_LABELS,
  type SleepStageKey,
} from '@/lib/utils/sleep';

/** One device's staged night. */
export interface HypnogramSeries {
  key: string;
  /** Rendered in the row gutter — usually a <DataSourceInfo />. */
  label: ReactNode;
  stages: SleepStage[];
}

interface HypnogramProps {
  series: HypnogramSeries[];
  className?: string;
}

/** A stage interval resolved to fractions of the shared time domain. */
interface Block {
  stage: SleepStageKey;
  start: number;
  end: number;
  left: number;
  width: number;
}

// The provider vocabularies carry more values than a hypnogram can usefully
// colour. `in_bed`, `sleeping` and `unknown` say "this device did not stage
// this epoch", which is exactly what bare surface already says — so they are
// dropped rather than given a colour that implies a stage was scored.
const PLOTTABLE = new Set<string>(HYPNOGRAM_STAGE_ORDER);

const isPlottable = (s: string): s is SleepStageKey => PLOTTABLE.has(s);

// Percent tolerance for float noise when deciding whether two blocks abut.
// 0.0001% of a night is well under a second, so a real unstaged gap — never
// shorter than one 30-second epoch — still shows through.
const ABUT_TOLERANCE = 0.0001;

/**
 * Paint a row as one hard-stop gradient rather than one element per interval.
 *
 * Adjacent absolutely-positioned elements antialias at fractional pixel edges
 * and let the track bleed through as a hairline, even when their geometry abuts
 * exactly. At 30-second epochs that artefact is indistinguishable from a real
 * gap, which here means "the device did not stage this epoch". One gradient is
 * a single paint context, so it cannot seam — and it is one DOM node per device
 * instead of several hundred.
 */
function gradient(blocks: Block[]): string | undefined {
  if (blocks.length === 0) return undefined;
  const stops: string[] = [];
  let cursor = 0;
  for (const b of blocks) {
    const right = b.left + b.width;
    if (b.left > cursor + ABUT_TOLERANCE) {
      stops.push(`transparent ${cursor}%`, `transparent ${b.left}%`);
    }
    const hex = SLEEP_STAGE_HEX[b.stage];
    stops.push(`${hex} ${b.left}%`, `${hex} ${right}%`);
    cursor = right;
  }
  if (cursor < 100) stops.push(`transparent ${cursor}%`, `transparent 100%`);
  return `linear-gradient(to right, ${stops.join(', ')})`;
}

/** Whole hours inside the domain, for the shared axis. */
function hourTicks(start: number, end: number): number[] {
  const ticks: number[] = [];
  const first = new Date(start);
  first.setMinutes(0, 0, 0);
  let t = first.getTime();
  if (t < start) t += 3_600_000;
  // Thin to every second hour once a night would otherwise crowd the axis.
  const step = end - start > 10 * 3_600_000 ? 7_200_000 : 3_600_000;
  for (; t <= end; t += step) ticks.push(t);
  return ticks;
}

export function Hypnogram({ series, className = '' }: HypnogramProps) {
  const [hover, setHover] = useState<{
    seriesKey: string;
    block: Block;
    x: number;
  } | null>(null);

  // One domain across every device, so a column is the same instant in every
  // row. Without this the rows cannot be read against each other, which is the
  // entire point of stacking them.
  const domain = useMemo(() => {
    let min = Infinity;
    let max = -Infinity;
    for (const s of series) {
      for (const stage of s.stages) {
        min = Math.min(min, new Date(stage.start_time).getTime());
        max = Math.max(max, new Date(stage.end_time).getTime());
      }
    }
    return Number.isFinite(min) && max > min ? { min, max } : null;
  }, [series]);

  const rows = useMemo(() => {
    if (!domain) return [];
    const span = domain.max - domain.min;
    return series.map((s) => {
      const blocks = s.stages.reduce<Block[]>((acc, stage) => {
        if (!isPlottable(stage.stage)) return acc;
        const start = new Date(stage.start_time).getTime();
        const end = new Date(stage.end_time).getTime();
        if (!(end > start)) return acc;
        acc.push({
          stage: stage.stage,
          start,
          end,
          left: ((start - domain.min) / span) * 100,
          width: ((end - start) / span) * 100,
        });
        return acc;
      }, []);

      blocks.sort((a, b) => a.start - b.start);
      return { ...s, blocks, fill: gradient(blocks) };
    });
  }, [series, domain]);

  if (!domain || rows.every((r) => r.blocks.length === 0)) {
    return (
      <p className="text-sm text-muted-foreground py-6 text-center">
        No staged sleep intervals from any source for this night.
      </p>
    );
  }

  const ticks = hourTicks(domain.min, domain.max);
  const span = domain.max - domain.min;

  return (
    <div className={`relative ${className}`}>
      {/* Legend — identity is never carried by colour alone, so each swatch is
          labelled and every row is named in its gutter. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mb-3">
        {HYPNOGRAM_STAGE_ORDER.map((stage) => (
          <span
            key={stage}
            className="flex items-center gap-1.5 text-xs text-muted-foreground"
          >
            <span
              className="h-2.5 w-2.5 rounded-sm"
              style={{ backgroundColor: SLEEP_STAGE_HEX[stage] }}
            />
            {SLEEP_STAGE_LABELS[stage]}
          </span>
        ))}
        <span className="text-xs text-muted-foreground/60">
          gaps = epochs the device did not stage
        </span>
      </div>

      <div className="space-y-1.5">
        {rows.map((row) => (
          <div key={row.key} className="flex items-center gap-3">
            <div className="w-40 shrink-0 truncate text-xs">{row.label}</div>
            <div
              className="relative h-7 flex-1 rounded-md bg-background/40"
              onMouseLeave={() => setHover(null)}
              onMouseMove={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                const frac = (e.clientX - rect.left) / rect.width;
                const t = domain.min + frac * span;
                const block = row.blocks.find((b) => t >= b.start && t < b.end);
                setHover(
                  block
                    ? { seriesKey: row.key, block, x: e.clientX - rect.left }
                    : null
                );
              }}
            >
              <div
                className="absolute inset-0 rounded-md"
                style={{ backgroundImage: row.fill }}
              />
              {hover?.seriesKey === row.key && (
                <div
                  className="pointer-events-none absolute -top-9 z-10 whitespace-nowrap rounded-md border border-border/60 bg-popover px-2 py-1 text-xs text-popover-foreground shadow-md"
                  style={{
                    left: `${hover.x}px`,
                    transform: 'translateX(-50%)',
                  }}
                >
                  <span
                    className="mr-1.5 inline-block h-2 w-2 rounded-sm align-middle"
                    style={{
                      backgroundColor: SLEEP_STAGE_HEX[hover.block.stage],
                    }}
                  />
                  {SLEEP_STAGE_LABELS[hover.block.stage]} ·{' '}
                  {format(hover.block.start, 'HH:mm')}–
                  {format(hover.block.end, 'HH:mm')} ·{' '}
                  {Math.round((hover.block.end - hover.block.start) / 60_000)}m
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Shared axis, drawn once under every row. */}
      <div className="flex items-center gap-3 mt-1">
        <div className="w-40 shrink-0" />
        <div className="relative h-4 flex-1">
          {ticks.map((t) => (
            <span
              key={t}
              className="absolute top-0 -translate-x-1/2 text-[10px] tabular-nums text-muted-foreground/70"
              style={{ left: `${((t - domain.min) / span) * 100}%` }}
            >
              {format(t, 'HH:mm')}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
