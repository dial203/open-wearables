import { describe, it, expect } from 'vitest';
import type { Device } from '@/lib/api/services/device.service';
import { sortDevicesByName } from './device-type';

const device = (id: string, display_name: string) =>
  ({ id, display_name }) as Device;

describe('sortDevicesByName', () => {
  it('orders by shown name, ignoring case and accents, numbers numerically', () => {
    const sorted = sortDevicesByName([
      device('1', 'WHOOP 5.0'),
      device('2', 'Garmin fenix 8'),
      device('3', 'apple Watch Ultra 3'),
      device('4', 'Garmin fēnix 6S Pro'),
      device('5', 'Oura Ring Gen 10'),
      device('6', 'Oura Ring Gen 4'),
      device('7', 'Garmin Fenix 6'),
    ]);
    expect(sorted.map((d) => d.display_name)).toEqual([
      'apple Watch Ultra 3',
      'Garmin Fenix 6',
      'Garmin fēnix 6S Pro',
      'Garmin fenix 8',
      'Oura Ring Gen 4',
      'Oura Ring Gen 10',
      'WHOOP 5.0',
    ]);
  });

  it('keeps same-named devices in their incoming order', () => {
    const sorted = sortDevicesByName([
      device('b', 'Garmin Venu X1'),
      device('z', 'Connect'),
      device('a', 'Garmin Venu X1'),
    ]);
    expect(sorted.map((d) => d.id)).toEqual(['z', 'b', 'a']);
  });

  it('does not mutate its input', () => {
    const input = [device('1', 'b'), device('2', 'a')];
    sortDevicesByName(input);
    expect(input.map((d) => d.id)).toEqual(['1', '2']);
  });
});
