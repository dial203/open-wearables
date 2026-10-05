/**
 * Display helpers for the raw `device_model` carried on a data source's
 * `source.device`. Never mutates the value — maps opaque hardware codes to
 * marketing names for display and infers a device kind for iconography.
 * Mirrors backend app/utils/device_registry.humanize_device_model.
 */

const APPLE_MODEL_NAMES: Record<string, string> = {
  'iPhone7,1': 'iPhone 6 Plus',
  'iPhone7,2': 'iPhone 6',
  'iPhone10,5': 'iPhone 8 Plus',
  'iPhone11,8': 'iPhone XR',
  'iPhone12,5': 'iPhone 11 Pro Max',
  'iPhone14,3': 'iPhone 13 Pro Max',
  'iPhone15,3': 'iPhone 14 Pro Max',
  'Watch3,4': 'Apple Watch Series 3',
  'Watch4,2': 'Apple Watch Series 4',
  'Watch6,2': 'Apple Watch Series 6',
  'Watch7,5': 'Apple Watch Series 8',
};

const SAMSUNG_MODEL_NAMES: Record<string, string> = {
  'SM-S901U': 'Galaxy S22',
  'SM-G973U1': 'Galaxy S10',
  'SM-G975U': 'Galaxy S10+',
  'SM-R830': 'Galaxy Watch Active2',
  'SM-Q501': 'Galaxy Ring',
  'LM-V350': 'LG V35 ThinQ',
};

/** Marketing name for an opaque hardware code, else the raw value unchanged. */
export function humanizeDeviceModel(
  device: string | null | undefined
): string | null {
  if (!device) return null;
  return APPLE_MODEL_NAMES[device] ?? SAMSUNG_MODEL_NAMES[device] ?? device;
}

export type DeviceKind = 'watch' | 'ring' | 'band' | 'phone' | 'other';

/** Best-effort device kind from the model string, for choosing an icon. */
export function inferDeviceKind(device: string | null | undefined): DeviceKind {
  if (!device) return 'other';
  const m = device.toLowerCase();
  if (
    /watch|fenix|forerunner|venu|epix|enduro|instinct|tactix|vantage|grit x|pacer|ignite|unite|vertical|suunto/.test(
      m
    )
  ) {
    return 'watch';
  }
  if (/ring|oura/.test(m) || m === 'sm-q501') return 'ring';
  if (/band|whoop|charge|inspire|vivosmart|vivofit/.test(m)) return 'band';
  if (/phone|^sm-[sg]|^lm-|pixel(?! watch)/.test(m)) return 'phone';
  return 'other';
}

/** The fields that decide what a device is called on screen. */
export interface DeviceNameParts {
  label: string | null;
  model_display: string | null;
  model_raw: string | null;
  brand: string | null;
  /** Hand-set brand, which wins over the one derived from the provider's report. */
  brand_display?: string | null;
  /** `auto` when detection guessed the label rather than a person setting it. */
  label_source?: string | null;
  device_type: string;
}

/**
 * What to call a device on screen, preferring the most human name available.
 *
 * A label a person set wins over everything, because it is the only one that can say
 * which of two identical units this is. An auto label ranks below the hand-set model:
 * detection names a relayed stream after the app that wrote it, and someone who then
 * types the model has said something the guess did not. `model_raw` is the provider's
 * verbatim string and is the last resort before a generic description — showing the raw
 * code beats guessing a friendlier name that might be wrong.
 *
 * The server derives the same name into `Device.display_name`; this exists for the
 * callers that hold the parts but not that field. Mirrors
 * backend/app/utils/device_naming.device_display_name.
 */
export function deviceDisplayName(
  device: DeviceNameParts,
  typeLabel: string
): string {
  if (device.label && device.label_source !== 'auto') return device.label;
  return (
    device.model_display ||
    device.label ||
    device.model_raw ||
    `${device.brand_display || device.brand || 'Unknown'} ${typeLabel}`
  );
}

/** The fields that decide what a source's device is called on screen. */
export interface SourceDeviceNameParts {
  device_display_name?: string | null;
  device_name?: string | null;
}

/**
 * What to call the device behind a sample.
 *
 * The registry's name wins: `device_name` is derived from whatever model string the
 * provider sent, and for a third-party app relaying through Apple Health or Health
 * Connect that string names the phone that ran the app, not the hardware that
 * recorded the data. Null when neither is known.
 */
export function sourceDeviceName(
  source: SourceDeviceNameParts | null | undefined
): string | null {
  return (
    source?.device_display_name?.trim() || source?.device_name?.trim() || null
  );
}
