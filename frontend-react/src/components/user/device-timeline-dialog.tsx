import { useMemo, useState } from 'react';
import { format } from 'date-fns';
import { Plus, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import {
  useDeviceTimeline,
  useRefileDeviceTimeline,
  useReplaceDeviceTimeline,
} from '@/hooks/api/use-health';
import type {
  DeviceRefileResult,
  DeviceTimeline,
  RefileCounts,
} from '@/lib/api/types';

interface Row {
  key: number;
  label: string;
  /** datetime-local value (browser's zone); '' with fromStart. */
  from: string;
  fromStart: boolean;
}

interface DeviceTimelineDialogProps {
  userId: string;
  connectionId: string;
  providerName: string;
  accountName: string;
  currentLabel: string | null | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const LOCAL_INPUT = "yyyy-MM-dd'T'HH:mm";

function toLocalInput(iso: string): string {
  return format(new Date(iso), LOCAL_INPUT);
}

function noonToday(): string {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  return format(d, LOCAL_INPUT);
}

function formatInstant(iso: string | null, fallback: string): string {
  return iso ? format(new Date(iso), 'd MMM yyyy, HH:mm') : fallback;
}

function rowsFrom(
  timeline: DeviceTimeline | undefined,
  currentLabel: string | null | undefined
): Row[] {
  if (timeline && timeline.periods.length > 0) {
    return timeline.periods.map((p, i) => ({
      key: i,
      label: p.device_label,
      from: p.effective_from ? toLocalInput(p.effective_from) : '',
      fromStart: p.effective_from === null,
    }));
  }
  // No history yet: start from what the account says now, worn from the start,
  // so adding the switch is one more row rather than retyping the old device.
  return [{ key: 0, label: currentLabel ?? '', from: '', fromStart: true }];
}

function validate(rows: Row[]): string | null {
  if (rows.some((r) => !r.label.trim())) return 'Every period needs a device.';
  if (rows.some((r, i) => r.fromStart && i !== 0))
    return 'Only the first period can run from the start.';
  const dated = rows.filter((r) => !r.fromStart);
  if (dated.some((r) => !r.from)) return 'Every later period needs a start.';
  const instants = dated.map((r) => new Date(r.from).getTime());
  if (instants.some((t) => Number.isNaN(t))) return 'A start is not a date.';
  if (new Set(instants).size !== instants.length)
    return 'Two periods cannot start at the same moment.';
  const sorted = [...instants].sort((a, b) => a - b);
  if (instants.some((t, i) => t !== sorted[i]))
    return 'List periods in order, earliest first.';
  return null;
}

function countsText(c: RefileCounts): string {
  const parts = [
    c.event_records && `${c.event_records} records`,
    c.samples && `${c.samples} samples`,
    c.archive_days && `${c.archive_days} archived days`,
    c.health_scores && `${c.health_scores} scores`,
  ].filter(Boolean);
  return parts.length ? parts.join(', ') : 'nothing';
}

function hasAny(c: RefileCounts): boolean {
  return c.event_records + c.samples + c.archive_days + c.health_scores > 0;
}

export function DeviceTimelineDialog({
  userId,
  connectionId,
  providerName,
  accountName,
  currentLabel,
  open,
  onOpenChange,
}: DeviceTimelineDialogProps) {
  const {
    data: timeline,
    isLoading,
    isError,
  } = useDeviceTimeline(userId, connectionId, open);
  const { mutate: saveTimeline, isPending: isSaving } =
    useReplaceDeviceTimeline(userId, connectionId);
  const { mutate: refile, isPending: isRefiling } = useRefileDeviceTimeline(
    userId,
    connectionId
  );

  // Rows are seeded from the server once per opening; `edited` tracks whether
  // they have diverged, because a re-file runs against the SAVED history.
  const [rows, setRows] = useState<Row[] | null>(null);
  const [edited, setEdited] = useState(false);
  const [preview, setPreview] = useState<DeviceRefileResult | null>(null);
  const [included, setIncluded] = useState<string[]>([]);

  const shown = rows ?? rowsFrom(timeline, currentLabel);
  const error = validate(shown);
  const saved = (timeline?.periods.length ?? 0) > 0;

  const unknownOrigin = useMemo(
    () =>
      (preview?.sources ?? []).filter(
        (s) => s.device_model_origin === null && s.device_model !== null
      ),
    [preview]
  );

  const reset = () => {
    setRows(null);
    setEdited(false);
    setPreview(null);
    setIncluded([]);
  };

  const update = (next: Row[]) => {
    setRows(next);
    setEdited(true);
    setPreview(null);
  };

  const runPreview = (include: string[]) =>
    refile(
      { dry_run: true, include_data_source_ids: include },
      { onSuccess: setPreview }
    );

  const handleSave = () =>
    saveTimeline(
      shown.map((r) => ({
        device_label: r.label.trim(),
        effective_from: r.fromStart ? null : new Date(r.from).toISOString(),
      })),
      {
        onSuccess: () => {
          setRows(null);
          setEdited(false);
          setPreview(null);
        },
      }
    );

  const handleApply = () =>
    refile(
      { dry_run: false, include_data_source_ids: included },
      {
        onSuccess: (result) => {
          setPreview(null);
          toast.success(`Re-filed ${countsText(result.total_moved)}`);
        },
      }
    );

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Device history — {accountName}</DialogTitle>
          <DialogDescription>
            Which device this {providerName} account was worn with, and from
            when. A record the provider sent without naming a device is filed
            under the device in effect at the record&apos;s own time — a sleep
            belongs to the device worn when it started, even if it synced later.
            A device the provider did name always wins.
          </DialogDescription>
        </DialogHeader>

        {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
        {isError && (
          <p className="text-sm text-destructive">
            Could not load this account&apos;s device history.
          </p>
        )}

        {!isLoading && !isError && (
          <div className="space-y-3">
            {shown.map((row, i) => (
              <div key={row.key} className="flex flex-wrap items-end gap-2">
                <div className="flex-1 min-w-[10rem] space-y-1">
                  <label
                    className="text-xs font-medium text-muted-foreground"
                    htmlFor={`period-label-${connectionId}-${row.key}`}
                  >
                    Device
                  </label>
                  <Input
                    id={`period-label-${connectionId}-${row.key}`}
                    value={row.label}
                    maxLength={100}
                    placeholder="e.g. Venu X1"
                    onChange={(e) =>
                      update(
                        shown.map((r) =>
                          r.key === row.key
                            ? { ...r, label: e.target.value }
                            : r
                        )
                      )
                    }
                  />
                </div>
                <div className="space-y-1">
                  <label
                    className="text-xs font-medium text-muted-foreground"
                    htmlFor={`period-from-${connectionId}-${row.key}`}
                  >
                    From
                  </label>
                  {row.fromStart ? (
                    <div className="h-9 flex items-center text-sm text-muted-foreground px-1">
                      the start of this account&apos;s data
                    </div>
                  ) : (
                    <Input
                      id={`period-from-${connectionId}-${row.key}`}
                      type="datetime-local"
                      value={row.from}
                      onChange={(e) =>
                        update(
                          shown.map((r) =>
                            r.key === row.key
                              ? { ...r, from: e.target.value }
                              : r
                          )
                        )
                      }
                    />
                  )}
                </div>
                {i === 0 && (
                  <label className="h-9 flex items-center gap-1.5 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={row.fromStart}
                      onChange={(e) =>
                        update(
                          shown.map((r) =>
                            r.key === row.key
                              ? {
                                  ...r,
                                  fromStart: e.target.checked,
                                  from: e.target.checked ? '' : noonToday(),
                                }
                              : r
                          )
                        )
                      }
                    />
                    from the start
                  </label>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-9 w-9"
                  aria-label="Remove period"
                  onClick={() => update(shown.filter((r) => r.key !== row.key))}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            ))}

            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                update([
                  ...shown,
                  {
                    key: Math.max(-1, ...shown.map((r) => r.key)) + 1,
                    label: '',
                    from: noonToday(),
                    fromStart: false,
                  },
                ])
              }
            >
              <Plus className="mr-1.5 h-4 w-4" />
              Add a switch
            </Button>

            <p className="text-[11px] text-muted-foreground">
              Times are in your browser&apos;s time zone. For a switch at
              bedtime, pick a time before that first night began. Before the
              first dated period, if nothing runs from the start, records are
              left with no device rather than guessed.
              {shown.length === 0 &&
                ' Saving with no periods removes the history; the account keeps its current device label.'}
            </p>
            {error && shown.length > 0 && (
              <p className="text-xs text-destructive">{error}</p>
            )}
          </div>
        )}

        {saved && !edited && (
          <div className="space-y-2 rounded-md border p-3">
            <div className="text-sm font-medium">Records already stored</div>
            <p className="text-xs text-muted-foreground">
              Saving the history does not move anything already stored. A
              re-file does, for this account only. It moves a record only off a
              device name a label supplied, never one the provider reported, and
              never onto a record already there.
            </p>
            {!preview && (
              <Button
                variant="outline"
                size="sm"
                disabled={isRefiling}
                onClick={() => runPreview(included)}
              >
                {isRefiling ? 'Checking…' : 'Preview re-file'}
              </Button>
            )}
            {preview && (
              <RefilePreview
                preview={preview}
                unknownOrigin={unknownOrigin}
                included={included}
                onToggle={(id, on) => {
                  const next = on
                    ? [...included, id]
                    : included.filter((x) => x !== id);
                  setIncluded(next);
                  runPreview(next);
                }}
              />
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          {preview && !edited && hasAny(preview.total_moved) && (
            <Button
              variant="secondary"
              disabled={isRefiling}
              onClick={handleApply}
            >
              {isRefiling
                ? 'Re-filing…'
                : `Re-file ${countsText(preview.total_moved)}`}
            </Button>
          )}
          <Button
            disabled={
              isSaving ||
              isLoading ||
              !edited ||
              (shown.length > 0 && error !== null)
            }
            onClick={handleSave}
          >
            {isSaving ? 'Saving…' : 'Save history'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RefilePreview({
  preview,
  unknownOrigin,
  included,
  onToggle,
}: {
  preview: DeviceRefileResult;
  unknownOrigin: DeviceRefileResult['sources'];
  included: string[];
  onToggle: (id: string, on: boolean) => void;
}) {
  return (
    <div className="space-y-2 text-xs">
      {preview.moves.length === 0 ? (
        <p className="text-muted-foreground">
          Nothing to move: every eligible record is already on the device its
          time names.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {preview.moves.map((m) => (
            <li
              key={`${m.from_data_source_id}-${m.to_device_model}-${m.period_start}`}
            >
              <span className="font-medium">
                {m.from_device_model ?? 'No device'} → {m.to_device_model}
              </span>{' '}
              <span className="text-muted-foreground">
                ({formatInstant(m.period_start, 'start')} –{' '}
                {formatInstant(m.period_end, 'now')})
              </span>
              : {countsText(m.moved)}
              {hasAny(m.conflicts) && (
                <span className="text-amber-500">
                  {' '}
                  · left in place, already at the destination:{' '}
                  {countsText(m.conflicts)}
                </span>
              )}
              {m.archive_days_straddling > 0 && (
                <span className="text-amber-500">
                  {' '}
                  · {m.archive_days_straddling} archived days span the switch
                  and stay whole
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {hasAny(preview.uncovered) && (
        <p className="text-muted-foreground">
          Not covered by any period, so not moved:{' '}
          {countsText(preview.uncovered)}.
        </p>
      )}
      {preview.sources
        .filter((s) => !s.eligible && s.device_model_origin === 'provider')
        .map((s) => (
          <p key={s.data_source_id} className="text-muted-foreground">
            “{s.device_model}” was named by the provider and is not moved.
          </p>
        ))}
      {unknownOrigin.length > 0 && (
        <div className="space-y-1">
          <p className="text-muted-foreground">
            These were stored before OW recorded whether a device name came from
            a label or from the provider. Tick one only if that name is a label
            someone typed, not one the provider sent — Garmin, for instance,
            names the watch on activities but never on sleep, so its activity
            sources must stay unticked.
          </p>
          {unknownOrigin.map((s) => (
            <label key={s.data_source_id} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={included.includes(s.data_source_id)}
                onChange={(e) => onToggle(s.data_source_id, e.target.checked)}
              />
              “{s.device_model}”
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
