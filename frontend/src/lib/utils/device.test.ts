import { describe, it, expect } from 'vitest';
import {
  deviceDisplayName,
  humanizeDeviceModel,
  inferDeviceKind,
  sourceDeviceName,
} from './device';

const base = {
  label: null,
  model_display: null,
  model_raw: null,
  brand: null,
  device_type: 'watch',
};

describe('deviceDisplayName', () => {
  it('prefers a name a person set', () => {
    // The only name that can distinguish two identical units on one account.
    expect(
      deviceDisplayName(
        {
          ...base,
          label: 'Sub 04 fenix',
          model_display: 'Garmin fenix 8',
          model_raw: 'fenix 8',
        },
        'Watch'
      )
    ).toBe('Sub 04 fenix');
  });

  it('falls back to the marketing name, then the raw model', () => {
    expect(
      deviceDisplayName(
        {
          ...base,
          model_display: 'Apple Watch Series 8',
          model_raw: 'Watch7,5',
        },
        'Watch'
      )
    ).toBe('Apple Watch Series 8');

    // Showing the provider's raw code beats guessing a friendlier one that is wrong.
    expect(deviceDisplayName({ ...base, model_raw: 'Watch7,9' }, 'Watch')).toBe(
      'Watch7,9'
    );
  });

  it('describes the device when nothing names it', () => {
    expect(deviceDisplayName({ ...base, brand: 'Oura' }, 'Ring')).toBe(
      'Oura Ring'
    );
    expect(deviceDisplayName(base, 'Watch')).toBe('Unknown Watch');
  });

  it('treats an empty label as no label', () => {
    expect(
      deviceDisplayName({ ...base, label: '', model_raw: 'fenix 8' }, 'Watch')
    ).toBe('fenix 8');
  });

  it('prefers a hand-set model over a label detection guessed', () => {
    // Detection names a relayed stream after the app that wrote it. Someone who then
    // types the model has said something the guess did not.
    expect(
      deviceDisplayName(
        {
          ...base,
          label: 'Muse',
          label_source: 'auto',
          model_display: 'Muse S Athena',
          device_type: 'eeg',
        },
        'EEG'
      )
    ).toBe('Muse S Athena');

    // ...but an auto label still beats no name at all.
    expect(
      deviceDisplayName(
        { ...base, label: 'Muse', label_source: 'auto', device_type: 'eeg' },
        'EEG'
      )
    ).toBe('Muse');
  });

  it('prefers a hand-set brand in the fallback description', () => {
    expect(
      deviceDisplayName(
        { ...base, brand: 'Apple', brand_display: 'Muse', device_type: 'eeg' },
        'EEG'
      )
    ).toBe('Muse EEG');
  });
});

describe('sourceDeviceName', () => {
  it('prefers the registry name over the provider model string', () => {
    // On a relayed stream `device_name` describes the phone that ran the writing app.
    expect(
      sourceDeviceName({
        device_display_name: 'Muse S Athena',
        device_name: 'iPhone 17 Pro',
      })
    ).toBe('Muse S Athena');
  });

  it('falls back to the provider model string, then to nothing', () => {
    expect(sourceDeviceName({ device_name: 'fenix 8' })).toBe('fenix 8');
    expect(sourceDeviceName({ device_display_name: '  ' })).toBeNull();
    expect(sourceDeviceName(null)).toBeNull();
  });
});

describe('humanizeDeviceModel', () => {
  it('maps a known hardware code and passes anything else through unchanged', () => {
    expect(humanizeDeviceModel('Watch6,2')).toBe('Apple Watch Series 6');
    expect(humanizeDeviceModel('fenix 8')).toBe('fenix 8');
    expect(humanizeDeviceModel(null)).toBeNull();
  });
});

describe('inferDeviceKind', () => {
  it('picks an icon kind from the model string', () => {
    expect(inferDeviceKind('Oura Ring Gen3')).toBe('ring');
    expect(inferDeviceKind('fenix 8')).toBe('watch');
    expect(inferDeviceKind('Whoop 5.0')).toBe('band');
    expect(inferDeviceKind('iPhone15,3')).toBe('phone');
    expect(inferDeviceKind(undefined)).toBe('other');
  });
});
