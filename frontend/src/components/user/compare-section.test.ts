import { describe, it, expect } from 'vitest';
import { buildSourceColumns } from './compare-section';
import type {
  RecoverySummary,
  SleepSummary,
  SourceMetadata,
} from '@/lib/api/types';

const DAY = '2026-09-25';

function source(overrides: Partial<SourceMetadata> = {}): SourceMetadata {
  return {
    provider: 'garmin',
    source: 'garmin',
    device: null,
    device_name: null,
    device_type: null,
    ...overrides,
  };
}

function sleep(src: SourceMetadata, minutes = 420): SleepSummary {
  return {
    date: DAY,
    source: src,
    duration_minutes: minutes,
  } as SleepSummary;
}

function recovery(src: SourceMetadata): RecoverySummary {
  return { date: DAY, source: src } as RecoverySummary;
}

// A model-less Garmin data source on one account, as both summaries now name it.
const left = source({ data_source_id: 'ds-left', user_connection_id: 'left' });
const right = source({
  data_source_id: 'ds-right',
  user_connection_id: 'right',
});

describe('buildSourceColumns', () => {
  it('joins a sleep summary and the recovery row from the same source', () => {
    const cols = buildSourceColumns([sleep(left)], [recovery(left)], DAY);

    expect(cols).toHaveLength(1);
    expect(cols[0].sleep).toBeDefined();
    expect(cols[0].recovery).toBeDefined();
    // Every row agrees, so the header can name the account and link the source.
    expect(cols[0].source?.data_source_id).toBe('ds-left');
    expect(cols[0].source?.user_connection_id).toBe('left');
  });

  it('keeps two accounts of one brand apart before any device is mapped', () => {
    const cols = buildSourceColumns(
      [sleep(left, 400), sleep(right, 380)],
      [recovery(left), recovery(right)],
      DAY
    );

    expect(cols).toHaveLength(2);
    expect(cols.map((c) => c.source?.user_connection_id).sort()).toEqual([
      'left',
      'right',
    ]);
    for (const col of cols) {
      expect(col.sleep?.source.data_source_id).toBe(col.source?.data_source_id);
      expect(col.recovery?.source.data_source_id).toBe(
        col.source?.data_source_id
      );
    }
  });

  it('keeps the data source on a column only that source reported into', () => {
    const polar = source({
      provider: 'polar',
      source: 'polar',
      data_source_id: 'ds-polar',
      user_connection_id: 'conn-polar',
    });
    const cols = buildSourceColumns([], [recovery(polar)], DAY);

    expect(cols).toHaveLength(1);
    expect(cols[0].source?.data_source_id).toBe('ds-polar');
    expect(cols[0].source?.user_connection_id).toBe('conn-polar');
  });

  it('names neither source when rows in one column disagree on it', () => {
    // A guard rather than a case the API produces today: the key is the source's
    // identity, so two rows sharing it should share a data source too.
    const cols = buildSourceColumns(
      [],
      [
        recovery(source({ data_source_id: 'ds-a', user_connection_id: 'a' })),
        recovery(source({ data_source_id: 'ds-b', user_connection_id: 'a' })),
      ],
      DAY
    );

    expect(cols).toHaveLength(1);
    expect(cols[0].source?.data_source_id ?? null).toBeNull();
  });

  it('separates two same-model sources once each is filed under a device', () => {
    const cols = buildSourceColumns(
      [
        sleep(source({ device_id: 'watch-left' })),
        sleep(source({ device_id: 'watch-right' })),
      ],
      [],
      DAY
    );

    expect(cols).toHaveLength(2);
  });

  it('leaves out rows filed under another day', () => {
    const other = { ...sleep(source()), date: '2026-09-24' };
    expect(buildSourceColumns([other], [], DAY)).toHaveLength(0);
  });
});
