import { describe, it, expect } from 'vitest';
import { sleepSessionDayKey } from './sleep';

describe('sleepSessionDayKey', () => {
  it('reads the calendar day off a full ISO timestamp', () => {
    // A session ending 07:41 local is attributed to that morning.
    expect(sleepSessionDayKey('2026-09-22T07:41:44')).toBe('2026-09-22');
  });

  it('handles an offset-bearing timestamp without splitting on the dashes', () => {
    // Regression: parseApiDate splits a date-only string on "-", so a timestamp
    // became [2026, 9, NaN] -> Invalid Date, and formatting it threw
    // "Invalid time value" and blanked the Compare tab.
    const key = sleepSessionDayKey('2026-09-22T11:41:44+00:00');
    expect(key).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(key).not.toBeNull();
  });

  it('survives a negative UTC offset, which carries its own dash', () => {
    const key = sleepSessionDayKey('2026-09-22T07:41:44-04:00');
    expect(key).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('returns null rather than throwing on an unusable value', () => {
    expect(sleepSessionDayKey('not a date')).toBeNull();
    expect(sleepSessionDayKey('')).toBeNull();
    expect(sleepSessionDayKey(null)).toBeNull();
    expect(sleepSessionDayKey(undefined)).toBeNull();
  });
});
