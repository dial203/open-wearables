import type { DeviceAttribution } from '@/lib/api/types';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

const OPTIONS: {
  value: DeviceAttribution;
  label: string;
  description: string;
}[] = [
  {
    value: 'per_record',
    label: "Each record's own device",
    description:
      'Every workout is filed under the watch, head unit or app its own metadata names, so one account can carry several devices. Use for an account that validation devices sync to.',
  },
  {
    value: 'single_device',
    label: 'One device for the whole account',
    description:
      'Every record is the device named below, whatever recorded it — a gold-standard account whose workouts were all recorded with a chest strap. The recorder each workout names is kept as provenance.',
  },
];

/**
 * The `sensor_label` to send for a choice. The API has no separate mode field:
 * naming the device is what makes an account a single device, and clearing it
 * is what makes it per-record again.
 */
export function sensorLabelFor(
  mode: DeviceAttribution,
  sensor: string
): string | null {
  return mode === 'single_device' ? sensor.trim() || null : null;
}

/** Whether a choice can be saved: a single device has to be named. */
export function isAttributionComplete(
  mode: DeviceAttribution,
  sensor: string
): boolean {
  return mode === 'per_record' || sensor.trim().length > 0;
}

interface DeviceAttributionFieldProps {
  id: string;
  providerName: string;
  mode: DeviceAttribution;
  sensor: string;
  onModeChange: (mode: DeviceAttribution) => void;
  onSensorChange: (sensor: string) => void;
}

export function DeviceAttributionField({
  id,
  providerName,
  mode,
  sensor,
  onModeChange,
  onSensorChange,
}: DeviceAttributionFieldProps) {
  return (
    <fieldset className="space-y-1.5">
      <legend className="text-xs font-medium text-muted-foreground">
        Device attribution
      </legend>
      <div className="space-y-1.5">
        {OPTIONS.map((option) => (
          <label
            key={option.value}
            htmlFor={`${id}-${option.value}`}
            className={cn(
              'flex cursor-pointer gap-2 rounded-md border px-3 py-2 text-sm',
              mode === option.value
                ? 'border-primary/60 bg-primary/5'
                : 'border-input'
            )}
          >
            <input
              id={`${id}-${option.value}`}
              type="radio"
              name={id}
              value={option.value}
              checked={mode === option.value}
              onChange={() => onModeChange(option.value)}
              className="mt-0.5"
            />
            <span>
              <span className="font-medium">{option.label}</span>
              <span className="block text-[11px] text-muted-foreground">
                {option.description}
              </span>
            </span>
          </label>
        ))}
      </div>
      {mode === 'single_device' && (
        <Input
          id={`${id}-sensor`}
          aria-label="Device behind every record"
          value={sensor}
          onChange={(e) => onSensorChange(e.target.value)}
          placeholder="e.g. Polar H10"
        />
      )}
      <p className="text-[11px] text-muted-foreground">
        Applies to this {providerName} account&apos;s existing data as well as
        future syncs. A source whose device someone edited by hand keeps that
        device.
      </p>
    </fieldset>
  );
}
