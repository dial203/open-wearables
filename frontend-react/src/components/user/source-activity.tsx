import { useMemo } from 'react';
import { Activity, Mail } from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  ACCOUNT_TYPE_CLASSES,
  UNCLASSIFIED_CLASSES,
} from '@/lib/utils/account';
import { accountTypeLabel } from '@/lib/api/types';
import type {
  ActivityBucket,
  SourceActivity,
} from '@/lib/api/services/device.service';

/**
 * What a source reported lately, and whose account it came through.
 *
 * This is the evidence for deciding what an unidentified device is. A source called
 * "Bluetooth Device" names nothing; fourteen nights of sleep and a readiness score
 * every morning name a ring or a band, and a run of cycling workouts names a head
 * unit. The account sits beside it as plain text rather than behind a hover, because
 * for a participant wearing two of one brand it is the other half of the question.
 */

/** Series codes are stored snake_case; nothing else about them is ours to change. */
function humanizeLabel(label: string): string {
  return label.replace(/_/g, ' ');
}

/** "14 nights", "3 workouts" - the noun a category is actually counted in. */
const EVENT_NOUNS: Record<string, [string, string]> = {
  sleep: ['night', 'nights'],
  nap: ['nap', 'naps'],
  workout: ['workout', 'workouts'],
  activity: ['activity', 'activities'],
  menstrual_cycle: ['cycle entry', 'cycle entries'],
};

function countLabel(bucket: ActivityBucket, nouns?: [string, string]): string {
  const n = bucket.count.toLocaleString();
  if (!nouns) return `${humanizeLabel(bucket.label)} ${n}`;
  return `${n} ${bucket.count === 1 ? nouns[0] : nouns[1]}`;
}

function relativeDay(iso: string): string {
  const then = new Date(iso).getTime();
  const hours = (Date.now() - then) / 36e5;
  if (hours < 1) return 'just now';
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? 'yesterday' : `${days}d ago`;
}

export function SourceActivityPanel({
  activity,
  windowDays,
  showAccount = true,
  className = '',
}: {
  activity: SourceActivity | undefined;
  windowDays: number;
  /** Off where the surrounding view already names the account. */
  showAccount?: boolean;
  className?: string;
}) {
  const summary = useMemo(() => {
    if (!activity) return [];
    const parts: string[] = [];
    for (const bucket of activity.events) {
      parts.push(countLabel(bucket, EVENT_NOUNS[bucket.label]));
    }
    for (const bucket of activity.scores) {
      parts.push(`${bucket.count} ${humanizeLabel(bucket.label)}`);
    }
    return parts;
  }, [activity]);

  if (!activity) return null;

  const hasAccount =
    showAccount &&
    Boolean(
      activity.account_label || activity.account_email || activity.account_type
    );
  const quiet = activity.last_seen_at === null;

  return (
    <div className={cn('space-y-1.5 text-xs', className)}>
      {hasAccount && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span
            className={cn(
              'rounded border px-1.5 py-0.5 text-[10px] font-medium',
              activity.account_type
                ? (ACCOUNT_TYPE_CLASSES[activity.account_type] ??
                    UNCLASSIFIED_CLASSES)
                : UNCLASSIFIED_CLASSES
            )}
          >
            {accountTypeLabel(activity.account_type)}
          </span>
          {activity.account_label && (
            <span className="text-foreground">{activity.account_label}</span>
          )}
          {/* Spelled out, not a tooltip: this is the record of which login a data
              set came from, and it has to be readable without hovering. */}
          {activity.account_email && (
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              <Mail className="h-3 w-3 shrink-0" />
              {activity.account_email}
            </span>
          )}
        </div>
      )}

      {quiet ? (
        <p className="text-muted-foreground">
          Nothing in the last {windowDays} days. Earlier data is untouched — a
          quiet source is a device that came off, or a pairing that broke.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-muted-foreground">
            <Activity className="h-3 w-3 shrink-0" />
            {summary.length > 0 ? (
              <span>{summary.join(' · ')}</span>
            ) : (
              <span>No sleep, workouts or scores — samples only</span>
            )}
            {activity.last_seen_at && (
              <span className="text-foreground/70">
                last {relativeDay(activity.last_seen_at)}
              </span>
            )}
          </div>
          {activity.metrics.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {activity.metrics.map((metric) => (
                <span
                  key={metric.label}
                  title={`${metric.count.toLocaleString()} samples, last ${relativeDay(metric.last_at)}`}
                  className="rounded bg-muted/50 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                >
                  {humanizeLabel(metric.label)}
                </span>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
