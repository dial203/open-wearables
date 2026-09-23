// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { Hypnogram } from './hypnogram';
import type { SleepStage } from '@/lib/api/types';

afterEach(cleanup);

// Shaped like the API's own output: ISO timestamps with an offset, run-length
// encoded, on a 30-second grid.
const muse: SleepStage[] = [
  {
    stage: 'awake',
    start_time: '2026-09-22T03:48:05+00:00',
    end_time: '2026-09-22T03:53:05+00:00',
  },
  {
    stage: 'light',
    start_time: '2026-09-22T03:53:05+00:00',
    end_time: '2026-09-22T05:10:05+00:00',
  },
  {
    stage: 'deep',
    start_time: '2026-09-22T05:10:05+00:00',
    end_time: '2026-09-22T06:04:05+00:00',
  },
  {
    stage: 'rem',
    start_time: '2026-09-22T06:04:05+00:00',
    end_time: '2026-09-22T06:54:05+00:00',
  },
];

const oura: SleepStage[] = [
  {
    stage: 'light',
    start_time: '2026-09-22T03:48:57+00:00',
    end_time: '2026-09-22T05:30:57+00:00',
  },
  // A genuine reporting gap — this device staged nothing here.
  {
    stage: 'rem',
    start_time: '2026-09-22T06:00:57+00:00',
    end_time: '2026-09-22T06:54:57+00:00',
  },
];

describe('Hypnogram', () => {
  it('renders a row per source over a shared domain', () => {
    render(
      <Hypnogram
        series={[
          { key: 'muse', label: 'Muse', stages: muse },
          { key: 'oura', label: 'Oura', stages: oura },
        ]}
      />
    );
    expect(screen.getByText('Muse')).toBeDefined();
    expect(screen.getByText('Oura')).toBeDefined();
    // Legend names every stage, so identity is never colour-alone.
    for (const label of ['Awake', 'REM', 'Light', 'Deep']) {
      expect(screen.getByText(label)).toBeDefined();
    }
  });

  it('paints each row as one gradient rather than an element per interval', () => {
    const { container } = render(
      <Hypnogram series={[{ key: 'muse', label: 'Muse', stages: muse }]} />
    );
    const fills = [
      ...container.querySelectorAll<HTMLElement>('[style*="linear-gradient"]'),
    ];
    expect(fills).toHaveLength(1);
    // Hard stops, and a stop pair per interval.
    expect(fills[0].style.backgroundImage).toContain('linear-gradient');
  });

  it('leaves a real reporting gap transparent rather than filling it', () => {
    const { container } = render(
      <Hypnogram series={[{ key: 'oura', label: 'Oura', stages: oura }]} />
    );
    const fill = container.querySelector<HTMLElement>(
      '[style*="linear-gradient"]'
    );
    expect(fill?.style.backgroundImage).toContain('transparent');
  });

  it('shows the empty state when no source staged anything plottable', () => {
    render(
      <Hypnogram
        series={[
          {
            key: 'whoop',
            label: 'Whoop',
            // Whoop relays in_bed/sleeping only — nothing a hypnogram can colour.
            stages: [
              {
                stage: 'in_bed',
                start_time: '2026-09-22T03:49:22+00:00',
                end_time: '2026-09-22T11:41:14+00:00',
              },
            ],
          },
        ]}
      />
    );
    expect(screen.getByText(/No staged sleep intervals/i)).toBeDefined();
  });

  it('does not throw on a malformed interval', () => {
    expect(() =>
      render(
        <Hypnogram
          series={[
            {
              key: 'bad',
              label: 'Bad',
              stages: [
                {
                  stage: 'light',
                  start_time: 'not a date',
                  end_time: 'also not',
                },
                ...muse,
              ],
            },
          ]}
        />
      )
    ).not.toThrow();
  });
});
