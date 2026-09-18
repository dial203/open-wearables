import { describe, expect, it } from 'vitest';
import { formatBuildTimestamp } from './build-info';

describe('formatBuildTimestamp', () => {
  it('formats a UTC timestamp', () => {
    expect(formatBuildTimestamp('2026-09-17T18:07:00Z')).toBe(
      '2026-09-17 18:07 UTC'
    );
  });

  it('converts an offset timestamp to UTC', () => {
    // git's %cI emits the committer's local offset; the footer must not shift
    // the date depending on where the build ran.
    expect(formatBuildTimestamp('2026-09-17T14:54:57-04:00')).toBe(
      '2026-09-17 18:54 UTC'
    );
  });

  it('pads single-digit components', () => {
    expect(formatBuildTimestamp('2026-01-02T03:04:05Z')).toBe(
      '2026-01-02 03:04 UTC'
    );
  });

  it('returns unknown for missing or unparseable input', () => {
    expect(formatBuildTimestamp('')).toBe('unknown');
    expect(formatBuildTimestamp(null)).toBe('unknown');
    expect(formatBuildTimestamp(undefined)).toBe('unknown');
    expect(formatBuildTimestamp('not a date')).toBe('unknown');
  });
});
