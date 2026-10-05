import { useMemo, useState } from 'react';
import { Loader2, Mail } from 'lucide-react';
import { Button } from '@/components/ui/button';
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
  DeviceTypeSelect,
  registryDeviceName,
} from '@/components/common/device-type';
import { providerLabel } from '@/components/common/source-badge';
import { SourceActivityPanel } from '@/components/user/source-activity';
import {
  useCreateDevice,
  useDevices,
  useLinkDataSource,
  useSourceActivity,
} from '@/hooks/api/use-devices';
import { useUserConnections } from '@/hooks/api/use-health';
import { buildAccountMap, type AccountDescriptor } from '@/lib/utils/account';

/**
 * The fields of a data source this dialog reads.
 *
 * Narrower than the data-source listing's row on purpose, so it can be opened from
 * anywhere a sample's `SourceMetadata` is on screen - a sleep session, a workout, a
 * recovery row - and not only from the Devices tab.
 */
export interface LinkableSource {
  id: string;
  provider: string;
  source: string | null;
  device_model: string | null;
  device_type: string | null;
  original_source_name: string | null;
  user_connection_id: string | null;
  /** The device it is filed under now; null when unattributed. */
  device_id: string | null;
}

const NEW_DEVICE = '__new__';

/** "Left wrist · account 2 of 3 · Validation" - the e-mail is shown beside it. */
function accountSummary(account: AccountDescriptor): string {
  const parts: string[] = [];
  if (account.name && account.name !== account.email) parts.push(account.name);
  parts.push(`account ${account.index} of ${account.count}`);
  parts.push(account.typeLabel);
  return parts.join(' · ');
}

const EMPTY_DRAFT = {
  device_type: 'unknown',
  label: '',
  brand: '',
  model_raw: '',
  wear_location: '',
};

/**
 * Attribute one data source to a device - an existing one, or one created here.
 *
 * Creating it here rather than sending someone to "Add device" first is the point: the
 * source you are looking at is the evidence for what the device is, and the case where
 * no device exists yet is the common one, not the exception. A ring arriving only as
 * `com.gdjztech.ringconn` has nothing to link to until someone makes it.
 *
 * Linking a source that already belongs to a device moves it; the backend records the
 * move in the device history like any other link.
 */
export function LinkDataSourceDialog({
  userId,
  source,
  onClose,
}: {
  userId: string;
  source: LinkableSource | null;
  onClose: () => void;
}) {
  const link = useLinkDataSource(userId);
  const create = useCreateDevice(userId);
  // Retired devices stay listed: a source's history can belong to a unit that has
  // since been replaced, and that unit is the right answer for it.
  const { data: deviceData } = useDevices(userId, true);
  const { data: connections } = useUserConnections(userId);
  const { data: activityData } = useSourceActivity(userId);
  const [deviceId, setDeviceId] = useState('');
  const [reason, setReason] = useState('');
  const [draft, setDraft] = useState(EMPTY_DRAFT);

  const devices = deviceData?.items;
  const sourceId = source?.id;
  const currentDeviceId = source?.device_id ?? null;
  const connectionId = source?.user_connection_id ?? null;
  const current = useMemo(
    () =>
      currentDeviceId
        ? devices?.find((d) => d.id === currentDeviceId)
        : undefined,
    [devices, currentDeviceId]
  );
  const account = useMemo(
    () =>
      connectionId ? buildAccountMap(connections).get(connectionId) : undefined,
    [connections, connectionId]
  );
  const activity = useMemo(
    () => activityData?.items.find((a) => a.data_source_id === sourceId),
    [activityData, sourceId]
  );

  const creating = deviceId === NEW_DEVICE;

  // Seeded from what the provider actually said, never from a guess: the canonical
  // brand it resolved and the model string it sent. Both stay editable, and an empty
  // seed is left empty rather than filled with the bundle id, which names an app.
  const startNewDevice = () => {
    setDeviceId(NEW_DEVICE);
    setDraft({
      ...EMPTY_DRAFT,
      device_type: source?.device_type ?? 'unknown',
      brand: source?.original_source_name ?? '',
      model_raw: source?.device_model ?? '',
    });
  };

  const close = () => {
    setDeviceId('');
    setReason('');
    setDraft(EMPTY_DRAFT);
    onClose();
  };

  const submit = async () => {
    if (!source) return;
    const why = reason || null;
    if (!creating) {
      if (!deviceId) return;
      link.mutate(
        { deviceId, dataSourceId: source.id, reason: why },
        { onSuccess: close }
      );
      return;
    }
    // Create then link. Sequential because the device id does not exist until the
    // first call returns; if the link fails the device is still there to link by hand,
    // which is recoverable, where inventing an id would not be.
    try {
      const device = await create.mutateAsync({
        device_type: draft.device_type,
        label: draft.label.trim() || null,
        brand: draft.brand.trim() || null,
        model_raw: draft.model_raw.trim() || null,
        wear_location: draft.wear_location.trim() || null,
        reason: why,
      });
      await link.mutateAsync({
        deviceId: device.id,
        dataSourceId: source.id,
        reason: why,
      });
      close();
    } catch {
      // Already reported: both mutations toast their own error. The dialog stays
      // open so the draft is not lost.
    }
  };

  const pending = link.isPending || create.isPending;
  const canSubmit = creating
    ? draft.device_type !== ''
    : !!deviceId && deviceId !== source?.device_id;

  return (
    <Dialog open={!!source} onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {source?.device_id ? 'Change device' : 'Link a data source'}
          </DialogTitle>
          <DialogDescription>
            Attribution groups data sources; it never combines them. The samples
            are untouched, and a direct read and an aggregator relay of the same
            unit stay separate so you can still compare them.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1 rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            <div>
              {source ? providerLabel(source.provider) : ''}
              {source?.source && source.source !== source.provider
                ? ` · ${source.source}`
                : ''}
              {source?.device_model
                ? ` · ${source.device_model}`
                : ' · no model reported'}
            </div>
            {source?.user_connection_id ? (
              <div className="space-y-0.5">
                <div className="flex items-center gap-1.5">
                  <Mail className="h-3 w-3 flex-shrink-0" />
                  {account?.email ? (
                    <span className="break-all text-foreground">
                      {account.email}
                    </span>
                  ) : (
                    <span className="italic">no e-mail on file</span>
                  )}
                </div>
                {account && (
                  <div className="pl-[18px]">{accountSummary(account)}</div>
                )}
              </div>
            ) : (
              source && <div>Not tied to a connected account</div>
            )}
            {source?.device_id && (
              <div>
                Currently filed under{' '}
                <span className="font-medium text-foreground">
                  {current ? registryDeviceName(current) : 'another device'}
                </span>
                . Linking it elsewhere moves it.
              </div>
            )}
          </div>

          <SourceActivityPanel
            activity={activity}
            windowDays={activityData?.window_days ?? 30}
            showAccount={false}
          />

          <p className="text-xs text-muted-foreground">
            This files the whole source under the device - everything it has
            delivered and will deliver, not only the record you opened it from.
            {source && !source.device_model && (
              <>
                {' '}
                It reports no model, so it holds every record this account sends
                without one; link it only if one device is behind all of them.
              </>
            )}
          </p>

          <div>
            <Label htmlFor="link-device">Device</Label>
            <select
              id="link-device"
              value={deviceId}
              onChange={(e) =>
                e.target.value === NEW_DEVICE
                  ? startNewDevice()
                  : setDeviceId(e.target.value)
              }
              className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">Select a device…</option>
              <option value={NEW_DEVICE}>+ Create a new device…</option>
              {(devices ?? []).map((d) => (
                <option
                  key={d.id}
                  value={d.id}
                  disabled={d.id === source?.device_id}
                >
                  {registryDeviceName(d)} ({d.data_sources.length} source
                  {d.data_sources.length === 1 ? '' : 's'})
                  {d.is_active ? '' : ' · retired'}
                  {d.id === source?.device_id ? ' · current' : ''}
                </option>
              ))}
            </select>
          </div>

          {creating && (
            <div className="space-y-3 rounded-md border border-border/60 bg-muted/20 p-3">
              <p className="text-xs text-muted-foreground">
                Seeded from what the provider reported. Everything here is
                editable, and a name you set is kept — detection never
                overwrites one.
              </p>
              <div>
                <Label htmlFor="link-new-type">Type</Label>
                <DeviceTypeSelect
                  id="link-new-type"
                  value={draft.device_type}
                  onChange={(v) => setDraft({ ...draft, device_type: v })}
                />
              </div>
              <div>
                <Label htmlFor="link-new-label">Name</Label>
                <Input
                  id="link-new-label"
                  value={draft.label}
                  onChange={(e) =>
                    setDraft({ ...draft, label: e.target.value })
                  }
                  placeholder="Sub 04 ring"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <Label htmlFor="link-new-brand">Brand</Label>
                  <Input
                    id="link-new-brand"
                    value={draft.brand}
                    onChange={(e) =>
                      setDraft({ ...draft, brand: e.target.value })
                    }
                    placeholder="RingConn"
                  />
                </div>
                <div>
                  <Label htmlFor="link-new-model">Model</Label>
                  <Input
                    id="link-new-model"
                    value={draft.model_raw}
                    onChange={(e) =>
                      setDraft({ ...draft, model_raw: e.target.value })
                    }
                    placeholder="Gen 2"
                  />
                </div>
              </div>
              <div>
                <Label htmlFor="link-new-wear">Wear location</Label>
                <Input
                  id="link-new-wear"
                  value={draft.wear_location}
                  onChange={(e) =>
                    setDraft({ ...draft, wear_location: e.target.value })
                  }
                  placeholder="left index finger"
                />
              </div>
            </div>
          )}

          <div>
            <Label htmlFor="link-reason">Reason (recorded in history)</Label>
            <Input
              id="link-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Muse headband, relayed through this phone"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button disabled={pending || !canSubmit || !source} onClick={submit}>
            {pending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : creating ? (
              'Create & link'
            ) : (
              'Link'
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
