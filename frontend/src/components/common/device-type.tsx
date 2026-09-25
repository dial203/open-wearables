import {
  Activity,
  Brain,
  CircleDashed,
  CircleDot,
  HelpCircle,
  Package,
  Scale,
  Smartphone,
  Vibrate,
  Watch,
  type LucideIcon,
} from 'lucide-react';
import type { DeviceType } from '@/lib/api/types';
import type { Device } from '@/lib/api/services/device.service';
import { deviceDisplayName } from '@/lib/utils/device';

const DEVICE_TYPE_INFO: Record<
  DeviceType,
  { label: string; Icon: LucideIcon }
> = {
  chest_strap: { label: 'Chest strap', Icon: Activity },
  eeg: { label: 'EEG', Icon: Brain },
  headband: { label: 'Headband', Icon: CircleDashed },
  watch: { label: 'Watch', Icon: Watch },
  band: { label: 'Band', Icon: Vibrate },
  ring: { label: 'Ring', Icon: CircleDot },
  phone: { label: 'Phone', Icon: Smartphone },
  scale: { label: 'Scale', Icon: Scale },
  other: { label: 'Other', Icon: Package },
  unknown: { label: 'Unknown', Icon: HelpCircle },
};

const FALLBACK = { label: 'Unknown', Icon: HelpCircle };

export function deviceTypeInfo(deviceType: DeviceType | string | null) {
  if (!deviceType) return FALLBACK;
  return DEVICE_TYPE_INFO[deviceType as DeviceType] ?? FALLBACK;
}

export function DeviceTypeIcon({
  deviceType,
  className = 'h-3.5 w-3.5',
}: {
  deviceType: DeviceType | string | null;
  className?: string;
}) {
  const { Icon } = deviceTypeInfo(deviceType);
  return <Icon className={className} aria-hidden />;
}

/** Every type a device can be given by hand, in the order a picker lists them. */
export const DEVICE_TYPES = [
  'chest_strap',
  'eeg',
  'headband',
  'watch',
  'band',
  'ring',
  'phone',
  'scale',
  'other',
  'unknown',
] as const;

export function DeviceTypeSelect({
  id,
  value,
  onChange,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <select
      id={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring"
    >
      {DEVICE_TYPES.map((type) => (
        <option key={type} value={type}>
          {deviceTypeInfo(type).label}
        </option>
      ))}
    </select>
  );
}

/**
 * What to call a registry device on screen.
 *
 * The server derives the same name into `display_name`; the local rule (lib/utils/device)
 * is the fallback for a response served before that field existed.
 */
export function registryDeviceName(device: Device): string {
  return (
    device.display_name ||
    deviceDisplayName(
      { ...device, device_type: String(device.device_type) },
      deviceTypeInfo(device.device_type).label
    )
  );
}
