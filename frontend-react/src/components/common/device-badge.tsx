import { Watch, CircleDot, Activity, Smartphone } from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  humanizeDeviceModel,
  inferDeviceKind,
  type DeviceKind,
} from '@/lib/utils/device';

interface DeviceBadgeProps {
  /** Raw device_model from source.device (may be null for providers that report none). */
  device: string | null | undefined;
  className?: string;
}

const KIND_ICON: Record<DeviceKind, React.ElementType> = {
  watch: Watch,
  ring: CircleDot,
  band: Activity,
  phone: Smartphone,
  other: CircleDot,
};

/**
 * Small chip showing which physical device produced the data (e.g. "Apple Watch
 * Series 6", "Galaxy Ring", "Versa 4"). Renders nothing when no device model is
 * available. The tooltip surfaces the raw hardware identifier, which is never
 * lost — useful since device models change often.
 */
export function DeviceBadge({ device, className = '' }: DeviceBadgeProps) {
  const label = humanizeDeviceModel(device);
  if (!label) return null;

  const kind = inferDeviceKind(device);
  const Icon = KIND_ICON[kind];
  const showRaw = label !== device;

  const chip = (
    <span
      className={cn(
        'inline-flex items-center gap-1 text-[10px] font-medium leading-none px-2 py-1 rounded-md border border-current/20 bg-muted/40 text-muted-foreground',
        className
      )}
    >
      <Icon className="h-3 w-3 flex-shrink-0" />
      <span className="max-w-[10rem] truncate">{label}</span>
    </span>
  );

  // Only wrap in a tooltip when the label differs from the raw code (so users
  // can still see the exact hardware identifier).
  if (!showRaw) return chip;

  return (
    <Tooltip>
      <TooltipTrigger asChild>{chip}</TooltipTrigger>
      <TooltipContent>
        <p>{device}</p>
      </TooltipContent>
    </Tooltip>
  );
}
