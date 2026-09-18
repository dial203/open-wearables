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
 * The fork's own version and last-updated time, resolved from (in order):
 *
 * 1. `FORK_*` env vars - for CI, which knows the commit without a checkout's git.
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

function gitForkMeta(): { commit?: string; updatedAt?: string } {
  const git = (...args: string[]) =>
    execFileSync('git', args, { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
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

function resolveForkMeta() {
  const stamp = readForkStamp();
  const fromGit = gitForkMeta();
  return {
    version: process.env.FORK_VERSION || stamp.version || 'unknown',
    commit: process.env.FORK_COMMIT || fromGit.commit || stamp.commit || 'unknown',
    updatedAt: process.env.FORK_UPDATED_AT || fromGit.updatedAt || stamp.updatedAt || '',
  };
}

const fork = resolveForkMeta();

const config = defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(version),
    __FORK_VERSION__: JSON.stringify(fork.version),
    __FORK_COMMIT__: JSON.stringify(fork.commit),
    __FORK_UPDATED_AT__: JSON.stringify(fork.updatedAt),
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
