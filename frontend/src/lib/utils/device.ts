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
