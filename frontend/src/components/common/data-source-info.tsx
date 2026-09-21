import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { SourceBadge, providerLabel } from '@/components/common/source-badge';
import {
  deviceTypeInfo,
  DeviceTypeIcon,
} from '@/components/common/device-type';
import { AccountChip } from '@/components/common/account-chip';
import { cn } from '@/lib/utils';
import { sourceDeviceName } from '@/lib/utils/device';
import type { SourceMetadata } from '@/lib/api/types';
import { type AccountDescriptor } from '@/lib/utils/account';

const NO_DEVICE_INFO = 'Device info not available';

export function DataSourceInfo({
  source,
  accounts,
  className = '',
}: {
  source: SourceMetadata | null | undefined;
  /**
   * The user's connected accounts, keyed by connection id. Pass it wherever
   * samples from several sources sit side by side: once a participant holds two
   * accounts with one provider, the badge and the device model are identical
   * across both and the account is the only thing that tells them apart.
   * Omitted, the component renders exactly as it did before.
   */
  accounts?: Map<string, AccountDescriptor>;
  className?: string;
}) {
  if (!source) return null;

  const account = source.user_connection_id
    ? accounts?.get(source.user_connection_id)
    : undefined;
  // Only when it would otherwise be ambiguous - a single-account user gains no
  // information from a chip saying which of their one account this is.
  const showAccount = account?.hasSiblings ?? false;

  const { label: deviceTypeLabel } = deviceTypeInfo(source.device_type);
  // The registry's name for the unit, falling back to whatever the provider's model
  // string described. Those differ whenever an app relayed through Apple Health or
  // Health Connect: there the provider's string is the phone that ran the app.
  const deviceName = sourceDeviceName(source);
  const reportedModel = source.device?.trim() || null;
  const isRelayed = Boolean(
    reportedModel && source.device_id && reportedModel !== deviceName
  );
  // Native API integrations store the provider key as the source ("garmin"/"garmin"),
  // so it only carries information for HealthKit / Health Connect writers.
  const showSource =
    source.source && source.source !== source.provider ? source.source : null;

  return (
    <div className={cn('flex min-w-0 items-center gap-1.5', className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="shrink-0">
            <SourceBadge provider={source.provider} />
          </span>
        </TooltipTrigger>
        <TooltipContent>
          Provider: {providerLabel(source.provider)}
        </TooltipContent>
      </Tooltip>

      {showAccount && account && <AccountChip account={account} />}

      {showSource && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="min-w-0 truncate text-[10px] text-muted-foreground">
              {showSource}
            </span>
          </TooltipTrigger>
          <TooltipContent>
            Written by <strong>{showSource}</strong> into{' '}
            {providerLabel(source.provider)}
          </TooltipContent>
        </Tooltip>
      )}

      <Tooltip>
        <TooltipTrigger asChild>
          <span className="flex min-w-0 items-center gap-1 text-[10px] text-muted-foreground">
            <DeviceTypeIcon
              deviceType={source.device_type}
              className="h-3 w-3 shrink-0"
            />
            <span className="truncate">{deviceName ?? NO_DEVICE_INFO}</span>
          </span>
        </TooltipTrigger>
        <TooltipContent>
          {deviceName ? (
            <div className="space-y-0.5">
              <div>
                {deviceTypeLabel}: {deviceName}
              </div>
              {reportedModel && reportedModel !== deviceName && (
                <div className="text-muted-foreground">
                  {isRelayed ? 'Reported by provider as' : 'Model'}:{' '}
                  {reportedModel}
                </div>
              )}
            </div>
          ) : (
            NO_DEVICE_INFO
          )}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}
