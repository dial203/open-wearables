/**
 * Build provenance for the version block in the sidebar footer.
 *
 * The values are baked in at build time by `vite.config.ts` (`__FORK_*`); this
 * module only formats them.
 */

/**
 * Format a build timestamp as `YYYY-MM-DD HH:MM UTC`.
 *
 * Deliberately UTC rather than the viewer's locale: the footer renders during
 * SSR and again on the client, and a locale/timezone-dependent string would
 * differ between the two and trip a hydration mismatch. UTC also matches the
 * convention in `docs/fork/DIVERGENCE.md`.
 *
 * Returns 'unknown' for a missing or unparseable timestamp - a build made
 * somewhere without git and without a stamp file still has to render.
 */
export function formatBuildTimestamp(iso: string | null | undefined): string {
  if (!iso) return 'unknown';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return 'unknown';

  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())} UTC`
  );
}
