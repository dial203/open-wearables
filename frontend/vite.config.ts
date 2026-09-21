import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import { devtools } from '@tanstack/devtools-vite';
import { tanstackStart } from '@tanstack/react-start/plugin/vite';
import viteReact from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { nitro } from 'nitro/vite';

// Upstream Open Wearables' version: package.json is synced from upstream, so
// this tracks whatever release this fork is rebased on.
const { version } = JSON.parse(readFileSync('./package.json', 'utf-8'));

/**
 * Fork and upstream build provenance, each resolved from (in order):
 *
 * 1. `FORK_*` / `UPSTREAM_*` env vars - for CI, which knows the commit without a
 *    checkout's git.
 * 2. git - a host checkout (`pnpm dev`, `pnpm build`), always exact and never stale.
 * 3. `fork-version.json` - the committed stamp from `make fork-stamp`. The Docker
 *    build context is `./frontend` with `.git` excluded, so images have only this.
 *
 * The version (semver) lives solely in the stamp file; git supplies commit and
 * timestamp. Anything unresolvable renders as a literal 'unknown' rather than
 * failing the build - a missing version string is not worth a broken image.
 */
function readForkStamp(): Record<string, string> {
  try {
    const parsed = JSON.parse(readFileSync('./fork-version.json', 'utf-8'));
    return typeof parsed === 'object' && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

const git = (...args: string[]) =>
  execFileSync('git', args, {
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'ignore'],
  }).trim();

function gitForkMeta(): { commit?: string; updatedAt?: string } {
  try {
    git('rev-parse', '--is-inside-work-tree');
    const dirty = git('status', '--porcelain').length > 0;
    return {
      commit: git('rev-parse', '--short', 'HEAD') + (dirty ? '-dirty' : ''),
      // Commit time, not build time: rebuilding an unchanged tree must not
      // move the "last updated" date.
      updatedAt: git('log', '-1', '--format=%cI', 'HEAD'),
    };
  } catch {
    return {};
  }
}

/**
 * The newest upstream commit this fork contains: the merge base with
 * `upstream/main`. Upstream's version number says which release we sit on, this
 * says which day of it - two syncs a month apart are both "v0.9.0". Needs the
 * `upstream` remote (`make fork-setup`); without it the stamp's value stands.
 */
function gitUpstreamMeta(): { commit?: string; updatedAt?: string } {
  try {
    git('rev-parse', '--is-inside-work-tree');
    const base = git('merge-base', 'upstream/main', 'HEAD');
    return {
      commit: git('rev-parse', '--short', base),
      updatedAt: git('log', '-1', '--format=%cI', base),
    };
  } catch {
    return {};
  }
}

function resolveForkMeta() {
  const stamp = readForkStamp();
  const fromGit = gitForkMeta();
  return {
    version: process.env.FORK_VERSION || stamp.version || 'unknown',
    commit:
      process.env.FORK_COMMIT || fromGit.commit || stamp.commit || 'unknown',
    updatedAt:
      process.env.FORK_UPDATED_AT || fromGit.updatedAt || stamp.updatedAt || '',
  };
}

function resolveUpstreamMeta() {
  const stamp = readForkStamp();
  const fromGit = gitUpstreamMeta();
  return {
    commit:
      process.env.UPSTREAM_COMMIT ||
      fromGit.commit ||
      stamp.upstreamCommit ||
      'unknown',
    updatedAt:
      process.env.UPSTREAM_UPDATED_AT ||
      fromGit.updatedAt ||
      stamp.upstreamUpdatedAt ||
      '',
  };
}

const fork = resolveForkMeta();
const upstream = resolveUpstreamMeta();

// When this bundle was built, as opposed to when the code it contains was
// written. The two diverge when a deployment is serving an older image than the
// branch it was built from, which is otherwise invisible from the UI.
const builtAt = new Date().toISOString();

const config = defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(version),
    __FORK_VERSION__: JSON.stringify(fork.version),
    __FORK_COMMIT__: JSON.stringify(fork.commit),
    __FORK_UPDATED_AT__: JSON.stringify(fork.updatedAt),
    __UPSTREAM_COMMIT__: JSON.stringify(upstream.commit),
    __UPSTREAM_UPDATED_AT__: JSON.stringify(upstream.updatedAt),
    __BUILT_AT__: JSON.stringify(builtAt),
  },
  build: {
    outDir: 'dist',
  },
  server: {
    host: '0.0.0.0',
    port: 3000,
    watch: {
      usePolling: true,
    },
  },
  resolve: {
    tsconfigPaths: true,
  },
  plugins: [
    devtools(),
    nitro({
      // decimal.js-light has "main": "decimal" (no extension) in package.json
      // which breaks ESM resolution when externalized. Force inline bundling.
      noExternals: ['decimal.js-light'],
    }),
    tailwindcss(),
    tanstackStart(),
    viteReact(),
  ],
});

export default config;
