// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { renderToString } from 'react-dom/server';
import {
  DeviceAttributionField,
  isAttributionComplete,
  sensorLabelFor,
} from './device-attribution-field';

function render(mode: 'per_record' | 'single_device', sensor = '') {
  return renderToString(
    <DeviceAttributionField
      id="attr"
      providerName="Strava"
      mode={mode}
      sensor={sensor}
      onModeChange={() => {}}
      onSensorChange={() => {}}
    />
  );
}

describe('sensorLabelFor', () => {
  // The API has no mode field: the label is the switch.
  it('sends the trimmed name for a single device', () => {
    expect(sensorLabelFor('single_device', '  Polar H10 ')).toBe('Polar H10');
  });

  it('clears the label for per-record attribution, whatever was typed', () => {
    expect(sensorLabelFor('per_record', 'Polar H10')).toBeNull();
  });
});

describe('isAttributionComplete', () => {
  it('refuses a single device with no name', () => {
    expect(isAttributionComplete('single_device', '   ')).toBe(false);
    expect(isAttributionComplete('single_device', 'Polar H10')).toBe(true);
  });

  it('needs nothing more for per-record attribution', () => {
    expect(isAttributionComplete('per_record', '')).toBe(true);
  });
});

describe('DeviceAttributionField', () => {
  it('offers both choices', () => {
    const html = render('per_record');
    expect(html).toContain('Each record&#x27;s own device');
    expect(html).toContain('One device for the whole account');
  });

  it('asks for the device only when the account is one device', () => {
    expect(render('per_record')).not.toContain('attr-sensor');
    expect(render('single_device', 'Polar H10')).toContain('attr-sensor');
  });
});
