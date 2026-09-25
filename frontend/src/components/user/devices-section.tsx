import { useMemo, useState } from 'react';
import {
  History,
  Link2,
  Link2Off,
  Loader2,
  Pencil,
  Plus,
  PowerOff,
  Power,
  Scissors,
  Sparkles,
  Merge,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { LoadingSpinner } from '@/components/common/loading-spinner';
import { ErrorState } from '@/components/common/error-state';
import {
  DeviceTypeIcon,
  DeviceTypeSelect,
  deviceTypeInfo,
  registryDeviceName,
} from '@/components/common/device-type';
import {
  useDevices,
  useCreateDevice,
  useUpdateDevice,
  useRetireDevice,
  useUnlinkDataSource,
  useSplitDevice,
  useMergeDevices,
  useLinkProposals,
  useRefreshProposals,
  useDecideProposal,
  useDeviceHistory,
  useSourceActivity,
} from '@/hooks/api/use-devices';
import type {
  Device,
  DeviceDataSource,
  SourceActivity,
} from '@/lib/api/services/device.service';
import { SourceActivityPanel } from '@/components/user/source-activity';
import { LinkDataSourceDialog } from '@/components/user/link-data-source-dialog';
import {
  useSetRelayVisibility,
  useUserDataSources,
} from '@/hooks/api/use-priorities';
import { useConfig } from '@/hooks/api/use-config';
import type { DataSource } from '@/lib/api/services/priority.service';
import { useUserConnections } from '@/hooks/api/use-health';
import { AccountChip } from '@/components/common/account-chip';
import { buildAccountMap, type AccountDescriptor } from '@/lib/utils/account';

// A stable empty array, so the `?? []` fallback below does not hand useMemo a new
// identity on every render and re-run the map build for nothing.
const NO_DEVICES: Device[] = [];
const NO_SOURCES: DataSource[] = [];

function formatWhen(value: string | null): string {
  if (!value) return '—';
  return new Date(value).toLocaleString();
}

export function DevicesSection({ userId }: { userId: string }) {
  const [includeRetired, setIncludeRetired] = useState(true);
  const { data, isLoading, error, refetch } = useDevices(
    userId,
    includeRetired
  );
  const { data: proposals } = useLinkProposals(userId);
  const { data: allSources } = useUserDataSources(userId);
  // Which account each source arrived through. The same physical unit re-paired
  // to a second account of one provider produces rows identical down to the
  // model string, so without this the registry cannot say which pairing a
  // stretch of data belongs to.
  const { data: connections } = useUserConnections(userId);
  const accounts = useMemo(() => buildAccountMap(connections), [connections]);
  // What each source has actually reported. Its own request rather than part of the
  // device listing: these are grouped aggregates over the largest tables in the
  // schema, and the page should render its names and structure without waiting.
  const { data: activityData } = useSourceActivity(userId);
  const activity = useMemo(() => {
    const map = new Map<string, SourceActivity>();
    for (const item of activityData?.items ?? [])
      map.set(item.data_source_id, item);
    return map;
  }, [activityData]);
  const windowDays = activityData?.window_days ?? 30;

  const [createOpen, setCreateOpen] = useState(false);
  const [linking, setLinking] = useState<DataSource | null>(null);
  const [editing, setEditing] = useState<Device | null>(null);
  const [splitting, setSplitting] = useState<Device | null>(null);
  const [merging, setMerging] = useState<Device | null>(null);
  const [historyFor, setHistoryFor] = useState<Device | 'all' | null>(null);

  const refreshProposals = useRefreshProposals(userId);

  const devices = data?.items ?? NO_DEVICES;
  const byId = useMemo(() => new Map(devices.map((d) => [d.id, d])), [devices]);

  // Sources belonging to no device. Normal for a provider that identifies no
  // hardware, and the only place a source someone detached can be put back.
  const unattributed = useMemo(
    () => (allSources ?? NO_SOURCES).filter((s) => !s.device_id),
    [allSources]
  );

  if (isLoading) return <LoadingSpinner size="lg" />;
  if (error) {
    return (
      <ErrorState
        title="Could not load devices"
        message={(error as Error).message}
        onRetry={() => refetch()}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h2 className="text-lg font-semibold">Devices</h2>
          <p className="text-sm text-muted-foreground">
            The physical units behind this user&apos;s data. Detection groups
            what a provider itself identifies and leaves the rest for you — an
            unassigned source is normal, not an error.
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={includeRetired}
              onChange={(e) => setIncludeRetired(e.target.checked)}
              className="accent-current"
            />
            Show retired
          </label>
          <Button
            variant="outline"
            size="sm"
            onClick={() => refreshProposals.mutate(undefined)}
            disabled={refreshProposals.isPending}
            className="gap-2"
          >
            {refreshProposals.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Sparkles className="h-4 w-4" />
            )}
            Find matches
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setHistoryFor('all')}
            className="gap-2"
          >
            <History className="h-4 w-4" />
            History
          </Button>
          <Button
            size="sm"
            onClick={() => setCreateOpen(true)}
            className="gap-2"
          >
            <Plus className="h-4 w-4" />
            Add device
          </Button>
        </div>
      </div>

      {!!proposals?.items.length && (
        <ProposalQueue
          userId={userId}
          devices={byId}
          proposals={proposals.items}
        />
      )}

      {devices.length === 0 ? (
        <Card className="p-8 text-center text-sm text-muted-foreground">
          No devices yet. They appear as data arrives from a provider that
          identifies its hardware, or you can add one by hand.
        </Card>
      ) : (
        <div className="space-y-3">
          {devices.map((device) => (
            <DeviceCard
              key={device.id}
              userId={userId}
              device={device}
              accounts={accounts}
              activity={activity}
              windowDays={windowDays}
              onEdit={() => setEditing(device)}
              onSplit={() => setSplitting(device)}
              onMerge={() => setMerging(device)}
              onHistory={() => setHistoryFor(device)}
            />
          ))}
        </div>
      )}

      <UnattributedSources
        userId={userId}
        sources={unattributed}
        accounts={accounts}
        activity={activity}
        windowDays={windowDays}
        onLink={(source) => setLinking(source)}
      />

      <CreateDeviceDialog
        userId={userId}
        open={createOpen}
        onOpenChange={setCreateOpen}
      />
      <LinkDataSourceDialog
        userId={userId}
        source={linking}
        onClose={() => setLinking(null)}
      />
      <EditDeviceDialog
        userId={userId}
        device={editing}
        onClose={() => setEditing(null)}
      />
      <SplitDeviceDialog
        userId={userId}
        device={splitting}
        onClose={() => setSplitting(null)}
      />
      <MergeDeviceDialog
        userId={userId}
        device={merging}
        candidates={devices}
        onClose={() => setMerging(null)}
      />
      <HistorySheet
        userId={userId}
        target={historyFor}
        onClose={() => setHistoryFor(null)}
      />
    </div>
  );
}

/**
 * The account a source arrived through, when the user holds more than one with
 * that provider. Silent otherwise, and silent for a one-time import, which
 * belongs to no account at all.
 */
function SourceAccount({
  connectionId,
  accounts,
}: {
  connectionId: string | null;
  accounts: Map<string, AccountDescriptor>;
}) {
  const account = connectionId ? accounts.get(connectionId) : undefined;
  if (!account?.hasSiblings) return null;
  return <AccountChip account={account} />;
}

/**
 * Data sources attributed to no device.
 *
 * Detection only groups what a provider itself identifies, so an unattributed source
 * is a normal resting state rather than an error - but until this panel existed there
 * was no way back from one. Unlinking was reachable from every device card and linking
 * was reachable from nowhere, so detaching a source by hand removed it from the
 * registry for good, however deliberate or accidental the detach was.
 */
function UnattributedSources({
  userId,
  sources,
  accounts,
  activity,
  windowDays,
  onLink,
}: {
  userId: string;
  sources: DataSource[];
  accounts: Map<string, AccountDescriptor>;
  activity: Map<string, SourceActivity>;
  windowDays: number;
  onLink: (source: DataSource) => void;
}) {
  const setRelayVisibility = useSetRelayVisibility();
  const config = useConfig();
  // Older backends do not send the flag, and the rule is on by default there too.
  const dedupEnabled = config.data?.relay_dedup_enabled !== false;

  if (sources.length === 0) return null;

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-medium">
          Unattributed sources ({sources.length})
        </h3>
        <p className="text-xs text-muted-foreground">
          Data still arrives and is stored; it just is not filed under a device
          yet.
        </p>
      </div>

      <ul className="mt-3 space-y-1">
        {sources.map((source) => (
          <li
            key={source.id}
            className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
          >
            <Link2Off className="h-3 w-3 flex-shrink-0" />
            <span className="truncate">
              {source.provider}
              {source.source ? ` · ${source.source}` : ''}
              {source.device_model ? ` · ${source.device_model}` : ''}
            </span>
            <SourceAccount
              connectionId={source.user_connection_id}
              accounts={accounts}
            />
            {source.attribution_locked_at && (
              <Badge
                variant="outline"
                title="Detached by hand. Detection leaves it alone until you link it again."
              >
                detached
              </Badge>
            )}
            {dedupEnabled &&
              source.redundant_relay &&
              source.relay_visibility === 'auto' && (
                <Badge
                  variant="outline"
                  title={`This brand also arrives directly from ${source.direct_provider ?? 'its maker'}. Reads leave this copy out for the span the direct connection covers; nothing is deleted.`}
                >
                  duplicate of {source.direct_provider ?? 'direct'}
                </Badge>
              )}
            {source.relay_visibility !== 'auto' && (
              <Badge
                variant="outline"
                title="Set by hand. The duplicate rule does not apply to this source."
              >
                {source.relay_visibility === 'always'
                  ? 'always shown'
                  : 'always hidden'}
              </Badge>
            )}
            {(source.redundant_relay || source.relay_visibility !== 'auto') && (
              <button
                type="button"
                disabled={setRelayVisibility.isPending}
                onClick={() =>
                  setRelayVisibility.mutate({
                    userId,
                    dataSourceId: source.id,
                    visibility:
                      source.relay_visibility === 'auto' ? 'always' : 'auto',
                  })
                }
                className="text-muted-foreground hover:text-foreground disabled:opacity-50"
                title={
                  source.relay_visibility === 'auto'
                    ? 'Keep this source in every read, whatever the duplicate rule says'
                    : 'Follow the duplicate rule again'
                }
              >
                {source.relay_visibility === 'auto' ? 'Keep it' : 'Reset'}
              </button>
            )}
            <button
              type="button"
              onClick={() => onLink(source)}
              className="ml-auto inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
              title="Attribute this source to a device"
            >
              <Link2 className="h-3 w-3" />
              Link
            </button>
            {/* The whole point of this panel: what the source reported is the only
                evidence of what the hardware is, and this is where someone decides. */}
            <div className="w-full pl-5">
              <SourceActivityPanel
                activity={activity.get(source.id)}
                windowDays={windowDays}
              />
            </div>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function DeviceCard({
  userId,
  device,
  accounts,
  activity,
  windowDays,
  onEdit,
  onSplit,
  onMerge,
  onHistory,
}: {
  userId: string;
  device: Device;
  accounts: Map<string, AccountDescriptor>;
  activity: Map<string, SourceActivity>;
  windowDays: number;
  onEdit: () => void;
  onSplit: () => void;
  onMerge: () => void;
  onHistory: () => void;
}) {
  const retire = useRetireDevice(userId);
  const unlink = useUnlinkDataSource(userId);
  const [showClaims, setShowClaims] = useState(false);
  const [showActivity, setShowActivity] = useState(false);

  const strongClaims = device.identities.filter(
    (i) => i.confidence === 'strong'
  );

  // The accounts this unit's data came through, in the order the sources list
  // them. Summarised in the header so "which pairing is this one?" is answerable
  // without expanding the sources - the question the registry could not answer
  // when the same unit had been paired to two accounts in turn.
  const deviceAccounts = useMemo(() => {
    const seen = new Map<string, AccountDescriptor>();
    for (const ds of device.data_sources) {
      const account = ds.user_connection_id
        ? accounts.get(ds.user_connection_id)
        : undefined;
      if (account?.hasSiblings) seen.set(account.id, account);
    }
    return [...seen.values()];
  }, [device.data_sources, accounts]);

  return (
    <Card className={device.is_active ? 'p-4' : 'p-4 opacity-60'}>
      <div className="flex flex-wrap items-start gap-3">
        <div className="mt-0.5 text-muted-foreground">
          <DeviceTypeIcon deviceType={device.device_type} className="h-5 w-5" />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{registryDeviceName(device)}</span>
            <Badge variant="outline">
              {deviceTypeInfo(device.device_type).label}
            </Badge>
            {device.label_source === 'manual' && (
              <Badge
                variant="secondary"
                title="Named by hand; detection will not overwrite it"
              >
                named
              </Badge>
            )}
            {!device.is_active && (
              <Badge variant="outline">
                retired {device.retired_at ? formatWhen(device.retired_at) : ''}
              </Badge>
            )}
            {strongClaims.length > 0 && (
              <Badge
                variant="secondary"
                title="A provider-issued identifier distinguishes this unit from an identical one"
              >
                identified
              </Badge>
            )}
          </div>

          <p className="mt-0.5 text-xs text-muted-foreground">
            {device.brand_display ?? device.brand ?? 'Unknown brand'}
            {device.model_raw ? ` · reported as "${device.model_raw}"` : ''}
            {/* The provider only ever described the phone that relayed this, so
                everything naming the unit itself was typed by a person. Saying so is
                what stops the name reading as something a provider confirmed. */}
            {device.host_model_raw
              ? ` · relayed by ${device.host_model_raw}`
              : ''}
            {device.wear_location ? ` · ${device.wear_location}` : ''}
            {device.serial ? ` · s/n ${device.serial}` : ''}
            {device.firmware_version ? ` · fw ${device.firmware_version}` : ''}
          </p>

          {deviceAccounts.length > 0 && (
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Paired via</span>
              {deviceAccounts.map((account) => (
                <AccountChip key={account.id} account={account} />
              ))}
            </div>
          )}

          <div className="mt-3 space-y-1">
            <p className="text-xs font-medium text-muted-foreground">
              Data sources ({device.data_sources.length})
            </p>
            {device.data_sources.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                None attributed yet.
              </p>
            ) : (
              <ul className="space-y-1">
                {device.data_sources.map((ds) => (
                  <li
                    key={ds.id}
                    className="flex items-center gap-2 text-xs text-muted-foreground"
                  >
                    <Link2 className="h-3 w-3 flex-shrink-0" />
                    <span className="truncate">
                      {ds.provider}
                      {ds.source ? ` · ${ds.source}` : ''}
                      {ds.device_model ? ` · ${ds.device_model}` : ''}
                    </span>
                    <SourceAccount
                      connectionId={ds.user_connection_id}
                      accounts={accounts}
                    />
                    <button
                      type="button"
                      onClick={() =>
                        unlink.mutate({
                          deviceId: device.id,
                          dataSourceId: ds.id,
                        })
                      }
                      disabled={unlink.isPending}
                      className="ml-auto inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
                      title="Detach this source from the device. The data is untouched."
                    >
                      <Link2Off className="h-3 w-3" />
                      Unlink
                    </button>
                    {showActivity && (
                      <div className="w-full pl-5 pt-0.5 pb-1">
                        <SourceActivityPanel
                          activity={activity.get(ds.id)}
                          windowDays={windowDays}
                        />
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {device.data_sources.length > 0 && (
            <div className="mt-2">
              <button
                type="button"
                onClick={() => setShowActivity((v) => !v)}
                className="text-xs text-muted-foreground underline-offset-2 hover:underline"
              >
                {showActivity ? 'Hide' : 'Show'} what it reported (last{' '}
                {windowDays} days)
              </button>
            </div>
          )}

          {device.identities.length > 0 && (
            <div className="mt-3">
              <button
                type="button"
                onClick={() => setShowClaims((v) => !v)}
                className="text-xs text-muted-foreground underline-offset-2 hover:underline"
              >
                {showClaims ? 'Hide' : 'Show'} how it is identified (
                {device.identities.length})
              </button>
              {showClaims && (
                <ul className="mt-2 space-y-1">
                  {device.identities.map((claim) => (
                    <li
                      key={`${claim.route}-${claim.id_kind}-${claim.id_value}`}
                      className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
                    >
                      <Badge variant="outline">{claim.route}</Badge>
                      <span>{claim.id_kind}</span>
                      <code className="truncate rounded bg-muted/50 px-1">
                        {claim.id_value}
                      </code>
                      <Badge
                        variant={
                          claim.confidence === 'strong'
                            ? 'secondary'
                            : 'outline'
                        }
                        title={
                          claim.confidence === 'strong'
                            ? 'Provider-issued and unique to this unit'
                            : 'Names the app or the model, which identical units share'
                        }
                      >
                        {claim.confidence}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-1">
          <Button variant="ghost" size="sm" onClick={onEdit} className="gap-1">
            <Pencil className="h-3.5 w-3.5" />
            Edit
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onSplit}
            disabled={device.data_sources.length < 2}
            title={
              device.data_sources.length < 2
                ? 'Needs at least two data sources to split'
                : 'Move some sources onto a separate device'
            }
            className="gap-1"
          >
            <Scissors className="h-3.5 w-3.5" />
            Split
          </Button>
          <Button variant="ghost" size="sm" onClick={onMerge} className="gap-1">
            <Merge className="h-3.5 w-3.5" />
            Merge
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() =>
              retire.mutate({ deviceId: device.id, retired: device.is_active })
            }
            disabled={retire.isPending}
            className="gap-1"
          >
            {device.is_active ? (
              <PowerOff className="h-3.5 w-3.5" />
            ) : (
              <Power className="h-3.5 w-3.5" />
            )}
            {device.is_active ? 'Retire' : 'Reactivate'}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onHistory}
            className="gap-1"
          >
            <History className="h-3.5 w-3.5" />
            History
          </Button>
        </div>
      </div>
    </Card>
  );
}

function ProposalQueue({
  userId,
  devices,
  proposals,
}: {
  userId: string;
  devices: Map<string, Device>;
  proposals: {
    id: string;
    device_a_id: string;
    device_b_id: string;
    score: string | number;
    evidence: Record<string, unknown> | null;
  }[];
}) {
  const decide = useDecideProposal(userId);

  return (
    <Card className="border-dashed p-4">
      <h3 className="text-sm font-medium">Possible same device</h3>
      <p className="mt-0.5 text-xs text-muted-foreground">
        These arrived by different routes and share no identifier, so this is a
        guess from overlapping sessions — not a match. Linking merges them and
        cannot be undone except by splitting; dismissing is permanent and the
        pair will not come back.
      </p>
      <ul className="mt-3 space-y-3">
        {proposals.map((proposal) => {
          const a = devices.get(proposal.device_a_id);
          const b = devices.get(proposal.device_b_id);
          const evidence = proposal.evidence ?? {};
          const overlap = evidence.overlap_fraction;
          const compared = evidence.sessions_compared;
          return (
            <li
              key={proposal.id}
              className="flex flex-wrap items-center gap-3 rounded-md border border-border/60 p-3"
            >
              <div className="min-w-0 flex-1 text-sm">
                <span className="font-medium">
                  {a ? registryDeviceName(a) : 'Unknown device'}
                </span>
                <span className="mx-2 text-muted-foreground">↔</span>
                <span className="font-medium">
                  {b ? registryDeviceName(b) : 'Unknown device'}
                </span>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  score {Number(proposal.score).toFixed(0)}
                  {typeof compared === 'number'
                    ? ` · ${compared} session${compared === 1 ? '' : 's'} compared`
                    : ''}
                  {typeof overlap === 'number'
                    ? ` · ${Math.round(overlap * 100)}% overlap`
                    : ''}
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    decide.mutate({ proposalId: proposal.id, accepted: false })
                  }
                  disabled={decide.isPending}
                >
                  Not the same
                </Button>
                <Button
                  size="sm"
                  onClick={() =>
                    decide.mutate({ proposalId: proposal.id, accepted: true })
                  }
                  disabled={decide.isPending}
                >
                  Same device
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function CreateDeviceDialog({
  userId,
  open,
  onOpenChange,
}: {
  userId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const create = useCreateDevice(userId);
  const [form, setForm] = useState({
    device_type: 'unknown',
    brand: '',
    model_raw: '',
    serial: '',
    firmware_version: '',
    label: '',
    wear_location: '',
    reason: '',
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add a device</DialogTitle>
          <DialogDescription>
            For hardware no provider has reported yet. A name you set here is
            kept — detection will not overwrite it.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="create-type">Type</Label>
            <DeviceTypeSelect
              value={form.device_type}
              onChange={(v) => setForm({ ...form, device_type: v })}
            />
          </div>
          <div>
            <Label htmlFor="create-label">Name</Label>
            <Input
              id="create-label"
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              placeholder="Sub 04 chest strap"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="create-brand">Brand</Label>
              <Input
                id="create-brand"
                value={form.brand}
                onChange={(e) => setForm({ ...form, brand: e.target.value })}
                placeholder="Polar"
              />
            </div>
            <div>
              <Label htmlFor="create-model">Model</Label>
              <Input
                id="create-model"
                value={form.model_raw}
                onChange={(e) =>
                  setForm({ ...form, model_raw: e.target.value })
                }
                placeholder="H10"
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="create-serial">Serial / asset tag</Label>
              <Input
                id="create-serial"
                value={form.serial}
                onChange={(e) => setForm({ ...form, serial: e.target.value })}
              />
            </div>
            <div>
              <Label htmlFor="create-firmware">Firmware</Label>
              <Input
                id="create-firmware"
                value={form.firmware_version}
                onChange={(e) =>
                  setForm({ ...form, firmware_version: e.target.value })
                }
              />
            </div>
          </div>
          <div>
            <Label htmlFor="create-wear">Wear location</Label>
            <Input
              id="create-wear"
              value={form.wear_location}
              onChange={(e) =>
                setForm({ ...form, wear_location: e.target.value })
              }
              placeholder="chest"
            />
          </div>
          <div>
            <Label htmlFor="create-reason">Reason (recorded in history)</Label>
            <Input
              id="create-reason"
              value={form.reason}
              onChange={(e) => setForm({ ...form, reason: e.target.value })}
              placeholder="criterion device for the validation arm"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={create.isPending}
            onClick={() =>
              create.mutate(
                {
                  device_type: form.device_type,
                  brand: form.brand || null,
                  model_raw: form.model_raw || null,
                  serial: form.serial || null,
                  firmware_version: form.firmware_version || null,
                  label: form.label || null,
                  wear_location: form.wear_location || null,
                  reason: form.reason || null,
                },
                { onSuccess: () => onOpenChange(false) }
              )
            }
          >
            {create.isPending ? 'Creating...' : 'Create'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EditDeviceDialog({
  userId,
  device,
  onClose,
}: {
  userId: string;
  device: Device | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={!!device} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        {/* Keyed by device id so opening a different device remounts the form with
            that device's values. Seeding state from props in an effect instead would
            cascade a render and can show the previous device's values for a frame. */}
        {device && (
          <EditDeviceForm
            key={device.id}
            userId={userId}
            device={device}
            onClose={onClose}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function EditDeviceForm({
  userId,
  device,
  onClose,
}: {
  userId: string;
  device: Device;
  onClose: () => void;
}) {
  const update = useUpdateDevice(userId);
  const [form, setForm] = useState({
    label: device.label ?? '',
    device_type: String(device.device_type),
    brand_display: device.brand_display ?? '',
    model_display: device.model_display ?? '',
    serial: device.serial ?? '',
    firmware_version: device.firmware_version ?? '',
    wear_location: device.wear_location ?? '',
    notes: device.notes ?? '',
    reason: '',
  });

  return (
    <>
      <DialogHeader>
        <DialogTitle>Edit device</DialogTitle>
        <DialogDescription>
          Brand and model here are what this device is called everywhere. They
          sit beside what the provider reported rather than replacing it — the
          original claim stays on the device as evidence of what the hardware
          said it was.
        </DialogDescription>
      </DialogHeader>
      <div className="space-y-3">
        <div>
          <Label htmlFor="edit-label">Name</Label>
          <Input
            id="edit-label"
            value={form.label}
            onChange={(e) => setForm({ ...form, label: e.target.value })}
            placeholder="Sub 04 headband"
          />
        </div>
        <div>
          <Label htmlFor="edit-type">Type</Label>
          <DeviceTypeSelect
            value={form.device_type}
            onChange={(v) => setForm({ ...form, device_type: v })}
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor="edit-brand">Brand</Label>
            <Input
              id="edit-brand"
              value={form.brand_display}
              onChange={(e) =>
                setForm({ ...form, brand_display: e.target.value })
              }
              placeholder={device.brand ?? 'Muse'}
            />
          </div>
          <div>
            <Label htmlFor="edit-model">Model</Label>
            <Input
              id="edit-model"
              value={form.model_display}
              onChange={(e) =>
                setForm({ ...form, model_display: e.target.value })
              }
              placeholder={device.model_raw ?? 'S Athena'}
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor="edit-serial">Serial / asset tag</Label>
            <Input
              id="edit-serial"
              value={form.serial}
              onChange={(e) => setForm({ ...form, serial: e.target.value })}
            />
          </div>
          <div>
            <Label htmlFor="edit-firmware">Firmware</Label>
            <Input
              id="edit-firmware"
              value={form.firmware_version}
              onChange={(e) =>
                setForm({ ...form, firmware_version: e.target.value })
              }
            />
          </div>
        </div>
        <div>
          <Label htmlFor="edit-wear">Wear location</Label>
          <Input
            id="edit-wear"
            value={form.wear_location}
            onChange={(e) =>
              setForm({ ...form, wear_location: e.target.value })
            }
          />
        </div>
        <div>
          <Label htmlFor="edit-notes">Notes</Label>
          <Input
            id="edit-notes"
            value={form.notes}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
          />
        </div>
        <div>
          <Label htmlFor="edit-reason">Reason (recorded in history)</Label>
          <Input
            id="edit-reason"
            value={form.reason}
            onChange={(e) => setForm({ ...form, reason: e.target.value })}
          />
        </div>
      </div>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button
          disabled={update.isPending}
          onClick={() =>
            update.mutate(
              {
                deviceId: device.id,
                data: {
                  label: form.label || null,
                  device_type: form.device_type,
                  brand_display: form.brand_display || null,
                  model_display: form.model_display || null,
                  serial: form.serial || null,
                  firmware_version: form.firmware_version || null,
                  wear_location: form.wear_location || null,
                  notes: form.notes || null,
                  reason: form.reason || null,
                },
              },
              { onSuccess: onClose }
            )
          }
        >
          {update.isPending ? 'Saving...' : 'Save'}
        </Button>
      </DialogFooter>
    </>
  );
}

function SplitDeviceDialog({
  userId,
  device,
  onClose,
}: {
  userId: string;
  device: Device | null;
  onClose: () => void;
}) {
  const split = useSplitDevice(userId);
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState('');

  const close = () => {
    setSelected([]);
    setReason('');
    onClose();
  };

  const toggle = (ds: DeviceDataSource) =>
    setSelected((prev) =>
      prev.includes(ds.id)
        ? prev.filter((id) => id !== ds.id)
        : [...prev, ds.id]
    );

  const sources = device?.data_sources ?? [];
  const wouldEmpty = sources.length > 0 && selected.length === sources.length;

  return (
    <Dialog open={!!device} onOpenChange={(open) => !open && close()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Split device</DialogTitle>
          <DialogDescription>
            Move the selected data sources onto a new device. Use this when two
            identical units were grouped as one, or to undo a merge.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {sources.map((ds) => (
            <label
              key={ds.id}
              className="flex items-center gap-2 rounded-md border border-border/60 p-2 text-sm"
            >
              <input
                type="checkbox"
                checked={selected.includes(ds.id)}
                onChange={() => toggle(ds)}
                className="accent-current"
              />
              <span className="truncate">
                {ds.provider}
                {ds.source ? ` · ${ds.source}` : ''}
                {ds.device_model ? ` · ${ds.device_model}` : ''}
              </span>
            </label>
          ))}
          {wouldEmpty && (
            <p className="text-xs text-destructive">
              Leave at least one source behind — moving all of them is a rename,
              not a split.
            </p>
          )}
          <div>
            <Label htmlFor="split-reason">Reason (recorded in history)</Label>
            <Input
              id="split-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="two identical watches on one account"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button
            disabled={
              split.isPending || selected.length === 0 || wouldEmpty || !device
            }
            onClick={() =>
              device &&
              split.mutate(
                {
                  deviceId: device.id,
                  dataSourceIds: selected,
                  reason: reason || null,
                },
                { onSuccess: close }
              )
            }
          >
            {split.isPending ? 'Splitting...' : 'Split'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MergeDeviceDialog({
  userId,
  device,
  candidates,
  onClose,
}: {
  userId: string;
  device: Device | null;
  candidates: Device[];
  onClose: () => void;
}) {
  const merge = useMergeDevices(userId);
  const [absorbId, setAbsorbId] = useState('');
  const [reason, setReason] = useState('');

  const close = () => {
    setAbsorbId('');
    setReason('');
    onClose();
  };

  const others = candidates.filter((d) => d.id !== device?.id);

  return (
    <Dialog open={!!device} onOpenChange={(open) => !open && close()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Merge into {device ? registryDeviceName(device) : ''}
          </DialogTitle>
          <DialogDescription>
            The other device is removed and its data sources move here. They are
            not combined — a direct read and an aggregator relay stay separate
            so you can still compare them. This cannot be undone except by
            splitting; everything needed to reverse it is kept in the history.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="merge-target">Device to absorb</Label>
            <select
              id="merge-target"
              value={absorbId}
              onChange={(e) => setAbsorbId(e.target.value)}
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">Select a device…</option>
              {others.map((d) => (
                <option key={d.id} value={d.id}>
                  {registryDeviceName(d)} ({d.data_sources.length} source
                  {d.data_sources.length === 1 ? '' : 's'})
                </option>
              ))}
            </select>
          </div>
          <div>
            <Label htmlFor="merge-reason">Reason (recorded in history)</Label>
            <Input
              id="merge-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="same ring, confirmed by session overlap"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button
            disabled={merge.isPending || !absorbId || !device}
            onClick={() =>
              device &&
              merge.mutate(
                {
                  keepDeviceId: device.id,
                  absorbDeviceId: absorbId,
                  reason: reason || null,
                },
                { onSuccess: close }
              )
            }
          >
            {merge.isPending ? 'Merging...' : 'Merge'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function HistorySheet({
  userId,
  target,
  onClose,
}: {
  userId: string;
  target: Device | 'all' | null;
  onClose: () => void;
}) {
  const deviceId = target && target !== 'all' ? target.id : undefined;
  const { data, isLoading } = useDeviceHistory(userId, deviceId, !!target);

  return (
    <Sheet open={!!target} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>
            {target && target !== 'all'
              ? `History — ${registryDeviceName(target)}`
              : 'Device history'}
          </SheetTitle>
          <SheetDescription>
            Every change to device attribution, newest first. Entries for a
            device that was merged away are kept — those are the ones that
            explain where its data went.
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-2 px-4 pb-6">
          {isLoading && <LoadingSpinner />}
          {!isLoading && !data?.items.length && (
            <p className="text-sm text-muted-foreground">
              Nothing recorded yet.
            </p>
          )}
          {data?.items.map((entry) => (
            <div
              key={entry.id}
              className="rounded-md border border-border/60 p-3 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline">{entry.action}</Badge>
                {entry.field && (
                  <span className="text-muted-foreground">{entry.field}</span>
                )}
                <span className="ml-auto text-muted-foreground">
                  {formatWhen(entry.created_at)}
                </span>
              </div>
              {(entry.old_value || entry.new_value) && (
                <p className="mt-1 break-all text-muted-foreground">
                  <span className="line-through">{entry.old_value ?? '—'}</span>
                  {' → '}
                  <span className="text-foreground">
                    {entry.new_value ?? '—'}
                  </span>
                </p>
              )}
              <p className="mt-1 text-muted-foreground">
                by {entry.actor ?? 'unknown'}
                {entry.reason ? ` · ${entry.reason}` : ''}
              </p>
            </div>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}
