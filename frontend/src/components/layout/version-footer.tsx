import { formatBuildTimestamp } from '@/lib/utils/build-info';

/**
 * Version block in the sidebar footer.
 *
 * This is a fork, so two versions matter and one line cannot say both: the
 * upstream Open Wearables release this is built on, and the fork's own version
 * plus when its code last moved. All three values are baked in at build time by
 * `vite.config.ts`; see `make fork-stamp` for how a Docker build gets them.
 */
export function VersionFooter() {
  const updated = formatBuildTimestamp(__FORK_UPDATED_AT__);
  const hasCommit = __FORK_COMMIT__ && __FORK_COMMIT__ !== 'unknown';

  return (
    <dl
      className="grid grid-cols-[auto_1fr] gap-x-2 px-3 text-[11px] leading-snug text-muted-foreground/50 select-none"
      title={[
        `Open Wearables (upstream): v${__APP_VERSION__}`,
        `Fork: v${__FORK_VERSION__}${hasCommit ? ` (${__FORK_COMMIT__})` : ''}`,
        `Fork last updated: ${updated}`,
      ].join('\n')}
    >
      <dt>OW</dt>
      <dd>v{__APP_VERSION__}</dd>

      <dt>fork</dt>
      <dd>
        v{__FORK_VERSION__}
        {hasCommit ? (
          <span className="text-muted-foreground/40"> · {__FORK_COMMIT__}</span>
        ) : null}
      </dd>

      <dt>updated</dt>
      <dd>{updated}</dd>
    </dl>
  );
}
