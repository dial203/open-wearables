import { describe, expect, it } from 'vitest';
import { filenameFromDisposition } from './download';

describe('filenameFromDisposition', () => {
  it('reads the quoted filename the API sends', () => {
    expect(
      filenameFromDisposition(
        'attachment; filename="P07-running-20260915-0130-wide.csv"',
        'fallback.csv'
      )
    ).toBe('P07-running-20260915-0130-wide.csv');
  });

  it('prefers the RFC 5987 form when present', () => {
    expect(
      filenameFromDisposition(
        `attachment; filename="plain.csv"; filename*=UTF-8''caf%C3%A9.csv`,
        'fallback.csv'
      )
    ).toBe('café.csv');
  });

  it('falls back when the header is missing or unreadable', () => {
    expect(filenameFromDisposition(null, 'fallback.csv')).toBe('fallback.csv');
    expect(filenameFromDisposition('attachment', 'fallback.csv')).toBe(
      'fallback.csv'
    );
  });
});
