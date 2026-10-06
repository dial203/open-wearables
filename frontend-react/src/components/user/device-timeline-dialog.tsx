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
  useDetectDeviceTimeline,
  useDeviceTimeline,
  useRefileDeviceTimeline,
  useReplaceDeviceTimeline,
} from '@/hooks/api/use-health';
import type {
  DeviceRefileResult,
  DeviceTimeline,
  RefileCounts,
} from '@/lib/api/types';
import { cn } from '@/lib/utils';

interface Row {
  key: number;
  label: string;
  /** datetime-local value (browser's zone); '' with fromStart. */
  from: string;
  fromStart: boolean;
  /**
   * The instant the row was seeded with. Sent back as-is while `from` still
   * shows it, so a detected start keeps its seconds and stays "detected".
   */
  seededIso: string | null;
  origin: 'detected' | 'stated';
  /** The previous device's last workout: the switch fell after it. */
  previousLastSeen: string | null;
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

function formatDay(iso: string): string {
  return format(new Date(iso), 'd MMM yyyy');
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
      seededIso: p.effective_from,
      origin: p.origin,
      previousLastSeen: p.previous_last_seen,
    }));
  }
  // Nothing saved yet: what the provider's own workouts show, if it names devices.
  const proposed = timeline?.detection.proposed ?? [];
  if (proposed.length > 0) {
    return proposed.map((p, i) => ({
      key: i,
      label: p.device_label,
      from: p.effective_from ? toLocalInput(p.effective_from) : '',
      fromStart: p.effective_from === null,
      seededIso: p.effective_from,
      origin: 'detected',
      previousLastSeen: p.previous_last_seen,
    }));
  }
  // No history and nothing to detect from: start from what the account says
  // now, worn from the start, so adding the switch is one more row.
  return [
    {
      key: 0,
      label: currentLabel ?? '',
      from: '',
      fromStart: true,
      seededIso: null,
      origin: 'stated',
      previousLastSeen: null,
    },
  ];
}

function startOf(row: Row): string | null {
  if (row.fromStart) return null;
  if (row.seededIso && toLocalInput(row.seededIso) === row.from) {
    return row.seededIso;
  }
  return new Date(row.from).toISOString();
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
  const { mutate: detect, isPending: isDetecting } = useDetectDeviceTimeline(
    userId,
    connectionId
  );

  // Rows are seeded from the server once per opening; `edited` tracks whether
  // they have diverged, because a re-file runs against the SAVED history.
  const [rows, setRows] = useState<Row[] | null>(null);
  const [edited, setEdited] = useState(false);
  const [auto, setAuto] = useState<boolean | null>(null);
  const [preview, setPreview] = useState<DeviceRefileResult | null>(null);
  const [included, setIncluded] = useState<string[]>([]);
  const [showDevices, setShowDevices] = useState(false);

  const shown = rows ?? rowsFrom(timeline, currentLabel);
  const error = validate(shown);
  const saved = (timeline?.periods.length ?? 0) > 0;
  const detection = timeline?.detection;
  const detects = detection?.supported ?? false;
  const proposedUnsaved = !saved && (detection?.proposed.length ?? 0) > 0;
  const autoShown = auto ?? timeline?.auto ?? true;
  const autoChanged = timeline !== undefined && autoShown !== timeline.auto;

  const workoutCount = useMemo(
    () => (detection?.devices ?? []).reduce((n, d) => n + d.evidence_count, 0),
    [detection]
  );

  // Sources a re-file leaves alone unless a person says the name was typed.
  const unknownOrigin = useMemo(
    () =>
      (preview?.sources ?? []).filter(
        (s) =>
          !s.eligible &&
          s.device_model_origin === null &&
          s.device_model !== null
      ),
    [preview]
  );

  const reset = () => {
    setRows(null);
    setEdited(false);
    setAuto(null);
    setPreview(null);
    setIncluded([]);
    setShowDevices(false);
  };

  const update = (next: Row[]) => {
    setRows(next);
    setEdited(true);
    setPreview(null);
  };

  const editRow = (key: number, change: Partial<Row>) =>
    update(
      shown.map((r) =>
        r.key === key
          ? { ...r, ...change, origin: 'stated', previousLastSeen: null }
          : r
      )
    );

  const runPreview = (include: string[]) =>
    refile(
      { dry_run: true, include_data_source_ids: include },
      { onSuccess: setPreview }
    );

  const handleSave = () =>
    saveTimeline(
      {
        periods:
          // Only the auto switch changed on an unsaved proposal: send no periods
          // rather than saving the proposal as if someone had typed it.
          !edited && !saved
            ? []
            : shown.map((r) => ({
                device_label: r.label.trim(),
                effective_from: startOf(r),
              })),
        auto: autoShown,
      },
      {
        onSuccess: () => {
          setRows(null);
          setEdited(false);
          setAuto(null);
          setPreview(null);
        },
      }
    );

  const handleDetect = () =>
    detect(
      { reset: true },
      {
        onSuccess: (result) => {
          setRows(null);
          setEdited(false);
          setPreview(null);
          if (!result.changed) {
            toast.info('The saved history already matches the workouts');
          } else if (result.refile && hasAny(result.refile.total_moved)) {
            toast.success(
              `History rebuilt; re-filed ${countsText(result.refile.total_moved)}`
            );
          } else {
            toast.success('History rebuilt from workouts');
          }
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

        {!isLoading && !isError && detects && detection && (
          <div className="space-y-2 rounded-md border p-3 text-xs">
            <p className="text-muted-foreground">
              {providerName} names the watch on every workout and on nothing
              else, so the workouts date each switch: a new device starts at its
              first workout.{' '}
              {detection.devices.length > 0 ? (
                <button
                  type="button"
                  className="underline underline-offset-2 hover:text-foreground"
                  onClick={() => setShowDevices((v) => !v)}
                >
                  {detection.devices.length} device
                  {detection.devices.length !== 1 ? 's' : ''} in {workoutCount}{' '}
                  workouts
                </button>
              ) : (
                'No workout on this account names a watch, band or ring yet.'
              )}
            </p>
            {showDevices && (
              <ul className="space-y-0.5 pl-1">
                {detection.devices.map((d) => (
                  <li key={`${d.device_label}-${d.first_seen}`}>
                    <span className="font-medium">{d.device_label}</span>{' '}
                    <span className="text-muted-foreground">
                      {formatDay(d.first_seen)} – {formatDay(d.last_seen)} ·{' '}
                      {d.evidence_count} workout
                      {d.evidence_count !== 1 ? 's' : ''}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={autoShown}
                onChange={(e) => setAuto(e.target.checked)}
              />
              Add new devices automatically, and re-file the records they cover
            </label>
            {proposedUnsaved && !edited && (
              <p className="text-muted-foreground">
                Below is what the workouts show; nothing is saved yet.
                {autoShown &&
                  ' It is applied automatically within a few minutes, or now with Apply.'}
              </p>
            )}
            {saved && detection.differs && (
              <p className="text-amber-500">
                The workouts show a different history from the one saved.
                Rebuilding replaces it, including anything entered by hand.
              </p>
            )}
          </div>
        )}

        {!isLoading && !isError && (
          <div className="space-y-3">
            {shown.map((row, i) => (
              <div key={row.key} className="space-y-1">
                <div className="flex flex-wrap items-end gap-2">
                  <div className="flex-1 min-w-[10rem] space-y-1">
                    <label
                      className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground"
                      htmlFor={`period-label-${connectionId}-${row.key}`}
                    >
                      Device
                      {detects && (
                        <span
                          className={cn(
                            'rounded border px-1 py-px text-[10px] font-normal',
                            row.origin === 'detected'
                              ? 'border-indigo-500/30 bg-indigo-500/10 text-indigo-400'
                              : 'border-border bg-muted text-muted-foreground'
                          )}
                        >
                          {row.origin === 'detected'
                            ? 'from workouts'
                            : 'entered'}
                        </span>
                      )}
                    </label>
                    <Input
                      id={`period-label-${connectionId}-${row.key}`}
                      value={row.label}
                      maxLength={100}
                      placeholder="e.g. Venu X1"
                      onChange={(e) =>
                        editRow(row.key, { label: e.target.value })
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
                          editRow(row.key, { from: e.target.value })
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
                          editRow(row.key, {
                            fromStart: e.target.checked,
                            from: e.target.checked ? '' : noonToday(),
                          })
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
                    onClick={() =>
                      update(shown.filter((r) => r.key !== row.key))
                    }
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
                {row.origin === 'detected' &&
                  row.previousLastSeen &&
                  !row.fromStart && (
                    <p className="text-[11px] text-muted-foreground">
                      Switched between the previous device&apos;s last workout (
                      {formatInstant(row.previousLastSeen, '')}) and this
                      one&apos;s first. Nights in that gap stay with the
                      previous device unless you move this start earlier.
                    </p>
                  )}
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
                    seededIso: null,
                    origin: 'stated',
                    previousLastSeen: null,
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
              {detects &&
                ' Saving your own edits keeps them as entered; after that, only switches in later workouts are added.'}
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
              {timeline?.auto && detects
                ? 'A history detected from workouts re-files on its own. A re-file here moves records to match a history you edited. '
                : 'Saving the history does not move anything already stored. A re-file does. '}
              It acts on this account only, never onto a record already there,
              and never moves a device name the provider reported
              {detects
                ? ` — on ${providerName}, that means workouts and the samples recorded during them stay where they are`
                : ''}
              .
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

        <DialogFooter className="gap-2 sm:gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
          {detects &&
            !edited &&
            (proposedUnsaved || (saved && detection?.differs)) && (
              <Button
                variant={proposedUnsaved ? 'default' : 'secondary'}
                disabled={isDetecting}
                onClick={handleDetect}
              >
                {isDetecting
                  ? 'Applying…'
                  : proposedUnsaved
                    ? 'Apply detected history'
                    : 'Rebuild from workouts'}
              </Button>
            )}
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
            variant={proposedUnsaved && !edited ? 'outline' : 'default'}
            disabled={
              isSaving ||
              isLoading ||
              !(edited || autoChanged) ||
              (edited && shown.length > 0 && error !== null)
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
              {m.archive_days_kept > 0 && (
                <span className="text-amber-500">
                  {' '}
                  · {m.archive_days_kept} archived days mix workout samples and
                  stay
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
            someone typed, not one the provider sent.
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
