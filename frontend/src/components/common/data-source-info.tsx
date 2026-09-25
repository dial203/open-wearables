import { useMemo, useState, type MouseEvent } from 'react';
import { Link2, Mail } from 'lucide-react';
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
import {
  LinkDataSourceDialog,
  type LinkableSource,
} from '@/components/user/link-data-source-dialog';
import { useUserConnections } from '@/hooks/api/use-health';
import { cn } from '@/lib/utils';
import { sourceDeviceName } from '@/lib/utils/device';
import type { SourceMetadata } from '@/lib/api/types';
import { buildAccountMap, type AccountDescriptor } from '@/lib/utils/account';

const NO_DEVICE_INFO = 'Device info not available';

// The row this component sits in is often itself clickable (an expandable session),
// and React bubbles events out of a portal to its React parent. Without this, a
// click inside the link dialog would also toggle the row underneath it.
const stop = (e: MouseEvent) => e.stopPropagation();

export function DataSourceInfo({
  source,
  accounts,
  userId,
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
  /**
   * The user the data belongs to. Passing it turns on the audit view: the login
   * e-mail of the account the data came through, spelled out rather than behind a
   * hover, and - where the row names its data source - a control to file that
   * source under a device without leaving the page. Omitted, neither appears.
   */
  userId?: string;
  className?: string;
}) {
  // Fetched only when the caller did not already resolve the accounts; every row
  // on a page shares the one cached request.
  const { data: connections } = useUserConnections(userId ?? '', !accounts);
  const fetchedAccounts = useMemo(
    () => buildAccountMap(connections),
    [connections]
  );
  const accountMap = accounts ?? fetchedAccounts;
  const [linking, setLinking] = useState(false);

  if (!source) return null;

  const account = source.user_connection_id
    ? accountMap.get(source.user_connection_id)
    : undefined;
  // Only when it would otherwise be ambiguous - a single-account user gains no
  // information from a chip saying which of their one account this is.
  const showAccount = account?.hasSiblings ?? false;
  const audit = Boolean(userId);

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

  // Attribution is per data source, so it needs the id. Daily aggregates group
  // several sources into one row and carry none; those stay read-only here.
  const linkable: LinkableSource | null =
    audit && source.data_source_id
      ? {
          id: source.data_source_id,
          provider: source.provider,
          source: source.source,
          device_model: reportedModel,
          device_type: source.device_type,
          original_source_name: source.original_source_name ?? null,
          user_connection_id: source.user_connection_id ?? null,
          device_id: source.device_id ?? null,
        }
      : null;

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

      {audit && (
        <AccountEmail
          provider={source.provider}
          connectionId={source.user_connection_id ?? null}
          // A daily aggregate pools sources and carries neither id, which says
          // nothing about whether an account was involved.
          perSource={Boolean(source.data_source_id)}
          account={account}
          // The chip already spells the e-mail out when it is the account's only name.
          hideEmail={
            showAccount && !!account?.email && account.name === account.email
          }
        />
      )}

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

      {linkable && userId && (
        <>
          <MapDeviceButton
            attributed={Boolean(linkable.device_id)}
            deviceName={deviceName}
            onClick={(e) => {
              stop(e);
              setLinking(true);
            }}
          />
          {/* Mounted only while open, so a page of rows does not fetch the device
              list once per row just to have a closed dialog ready. */}
          {linking && (
            <span onClick={stop} className="contents">
              <LinkDataSourceDialog
                userId={userId}
                source={linkable}
                onClose={() => setLinking(false)}
              />
            </span>
          )}
        </>
      )}
    </div>
  );
}

/**
 * The login e-mail of the account a row came through, in plain text.
 *
 * Plain text rather than a tooltip because this is what an audit reads down a column
 * of rows. Where there is nothing to show, it says why instead of going silent: a
 * missing e-mail and a row tied to no account are both things someone checking the
 * data wants to see.
 */
function AccountEmail({
  provider,
  connectionId,
  perSource,
  account,
  hideEmail,
}: {
  provider: string;
  connectionId: string | null;
  perSource: boolean;
  account: AccountDescriptor | undefined;
  hideEmail: boolean;
}) {
  if (!connectionId) {
    if (!perSource) return null;
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="shrink-0 text-[10px] italic text-muted-foreground">
            no account
          </span>
        </TooltipTrigger>
        <TooltipContent>
          Not tied to a connected account: a one-time file import, or a row
          stored before accounts were tracked.
        </TooltipContent>
      </Tooltip>
    );
  }
  // Still loading. A deleted account nulls the id rather than leaving it dangling.
  if (!account || hideEmail) return null;

  const where = `${providerLabel(provider)} account ${account.index} of ${account.count} · ${account.typeLabel}`;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {account.email ? (
          <span className="flex min-w-0 max-w-[16rem] items-center gap-1 text-[10px] text-muted-foreground">
            <Mail className="h-3 w-3 shrink-0" />
            <span className="truncate">{account.email}</span>
          </span>
        ) : (
          <span className="flex shrink-0 items-center gap-1 text-[10px] italic text-muted-foreground">
            <Mail className="h-3 w-3 shrink-0" />
            no e-mail on file
          </span>
        )}
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <div>{where}</div>
          {account.name && account.name !== account.email && (
            <div className="text-muted-foreground">{account.name}</div>
          )}
          {!account.email && (
            <div className="text-muted-foreground">
              Set the login e-mail on this account&apos;s card in the Profile
              tab.
            </div>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Opens the link dialog for this row's data source.
 *
 * Loud when the source is unattributed - that is the gap someone auditing a page is
 * looking for - and a quiet icon once it has a device, where it is only a correction.
 */
function MapDeviceButton({
  attributed,
  deviceName,
  onClick,
}: {
  attributed: boolean;
  deviceName: string | null;
  onClick: (e: MouseEvent<HTMLButtonElement>) => void;
}) {
  if (!attributed) {
    return (
      <button
        type="button"
        onClick={onClick}
        className="inline-flex shrink-0 items-center gap-1 rounded border border-dashed border-warning-muted/50 px-1.5 py-0.5 text-[10px] font-medium text-warning-muted transition-colors hover:bg-warning-muted/10"
        title="File this data source under a device"
      >
        <Link2 className="h-3 w-3" />
        Map device
      </button>
    );
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          aria-label="Change device"
          className="shrink-0 rounded p-0.5 text-muted-foreground/60 transition-colors hover:bg-muted/50 hover:text-foreground"
        >
          <Link2 className="h-3 w-3" />
        </button>
      </TooltipTrigger>
      <TooltipContent>
        Filed under {deviceName ?? 'a device'}. Change which device this source
        belongs to.
      </TooltipContent>
    </Tooltip>
  );
}
