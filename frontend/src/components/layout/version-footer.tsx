import { formatBuildTimestamp } from '@/lib/utils/build-info';

function Row({
  label,
  version,
  commit,
  updatedAt,
}: {
  label: string;
  version: string;
  commit: string;
  updatedAt: string;
}) {
  const hasCommit = commit && commit !== 'unknown';

  return (
    <dl className="grid grid-cols-[2.5rem_1fr]">
      <dt className="row-span-2 text-muted-foreground/60">{label}</dt>
      <dd>
        v{version}
        {hasCommit ? (
          <span className="text-muted-foreground/40"> · {commit}</span>
        ) : null}
      </dd>
      <dd className="text-muted-foreground/40">
        {formatBuildTimestamp(updatedAt)}
      </dd>
    </dl>
  );
}

/**
 * Version block in the sidebar footer.
 *
 * This is a fork, so two versions matter and one line cannot say both: the
 * upstream Open Wearables release this is built on, and the fork's own version.
 * Each carries the date its code last moved — upstream's is the newest upstream
 * commit merged in, so two syncs a month apart are distinguishable even though
 * both are "v0.9.0". All values are baked in at build time by `vite.config.ts`;
 * see `make fork-stamp` for how a Docker build gets them.
 */
export function VersionFooter() {
  return (
    <div
      className="space-y-1 px-3 text-[11px] leading-snug text-muted-foreground/50 select-none"
      title={[
        `Open Wearables (upstream): v${__APP_VERSION__} (${__UPSTREAM_COMMIT__})`,
        `  last upstream commit merged in: ${formatBuildTimestamp(__UPSTREAM_UPDATED_AT__)}`,
        `Fork: v${__FORK_VERSION__} (${__FORK_COMMIT__})`,
        `  last updated: ${formatBuildTimestamp(__FORK_UPDATED_AT__)}`,
        `Built: ${formatBuildTimestamp(__BUILT_AT__)}`,
      ].join('\n')}
    >
      <Row
        label="OW"
        version={__APP_VERSION__}
        commit={__UPSTREAM_COMMIT__}
        updatedAt={__UPSTREAM_UPDATED_AT__}
      />
      <Row
        label="fork"
        version={__FORK_VERSION__}
        commit={__FORK_COMMIT__}
        updatedAt={__FORK_UPDATED_AT__}
      />
    </div>
  );
}
