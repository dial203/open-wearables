/** Upstream Open Wearables version, from `package.json` (synced from upstream). */
declare const __APP_VERSION__: string;

/** Newest upstream commit this fork contains (its merge base with `upstream/main`). */
declare const __UPSTREAM_COMMIT__: string;

/** ISO-8601 commit time of that upstream commit, or '' when it could not be resolved. */
declare const __UPSTREAM_UPDATED_AT__: string;

/** This fork's own version, from `fork-version.json`. */
declare const __FORK_VERSION__: string;

/** Short commit this build was made from, with a `-dirty` suffix for an unclean tree. */
declare const __FORK_COMMIT__: string;

/** ISO-8601 commit time of that commit, or '' when it could not be resolved. */
declare const __FORK_UPDATED_AT__: string;

/** ISO-8601 time this bundle was built, which is not when its code was written. */
declare const __BUILT_AT__: string;
