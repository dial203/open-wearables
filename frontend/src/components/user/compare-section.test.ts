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

// A sleep summary is an aggregate: the API never gives it a data source or account.
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

describe('buildSourceColumns', () => {
  it('joins a sleep summary and the recovery row from the same source', () => {
    // The recovery row names its data source; the sleep summary cannot. Keying on
    // the id would split one Garmin into a sleep column and a recovery column.
    const cols = buildSourceColumns(
      [sleep(source())],
      [
        recovery(
          source({ data_source_id: 'ds-1', user_connection_id: 'conn-1' })
        ),
      ],
      DAY
    );

    expect(cols).toHaveLength(1);
    expect(cols[0].sleep).toBeDefined();
    expect(cols[0].recovery).toBeDefined();
  });

  it('names no data source when the sleep summary may be pooling several', () => {
    const cols = buildSourceColumns(
      [sleep(source())],
      [
        recovery(
          source({ data_source_id: 'ds-1', user_connection_id: 'conn-1' })
        ),
      ],
      DAY
    );

    expect(cols[0].source?.data_source_id ?? null).toBeNull();
    expect(cols[0].source?.user_connection_id ?? null).toBeNull();
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

  it('names neither source when two land in one column', () => {
    const cols = buildSourceColumns(
      [],
      [
        recovery(source({ data_source_id: 'ds-a', user_connection_id: 'a' })),
        recovery(source({ data_source_id: 'ds-b', user_connection_id: 'b' })),
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
