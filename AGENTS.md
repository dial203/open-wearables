# Open Wearables

Open Wearables is a health/wearable data aggregation platform with a Python/FastAPI backend and React/TypeScript frontend.

## Documentation Structure

- **This file** - Project overview, development workflow, general guidelines
- **[backend/AGENTS.md](backend/AGENTS.md)** - Backend-specific patterns and code examples
- **[frontend/AGENTS.md](frontend/AGENTS.md)** - Frontend-specific patterns and code examples
- **[mcp/README.md](mcp/README.md)** - MCP server setup and available tools
- **[docs/dev-guides/how-to-add-new-provider.mdx](docs/dev-guides/how-to-add-new-provider.mdx)** - Adding wearable providers

## Project Structure

```
open-wearables/
├── backend/           # Python/FastAPI backend
├── frontend/          # React/TypeScript frontend
├── mcp/               # MCP server for AI assistants
└── docs/              # Documentation (Mintlify)
```

## Tech Stack

| Backend | Frontend | MCP |
|---------|----------|-----|
| Python 3.14+ | React 19 + TypeScript | Python 3.13+ |
| FastAPI | TanStack Router/Query | FastMCP |
| SQLAlchemy 2.0 | React Hook Form + Zod | httpx |
| PostgreSQL | Tailwind + shadcn/ui | |
| Celery + Redis | Vitest | |
| Ruff + ty | oxlint + Prettier | Ruff + ty |

## Development Workflow

### Docker (Recommended)

```bash
# Start all services
docker compose up -d

# Admin account and series type definitions are auto-created on startup (admin@admin.com / your-secure-password)
# Seed sample test data (optional)
make seed

# View logs
docker compose logs -f app

# Stop
make stop
```

### Access Points
- Frontend: http://localhost:3000
- API: http://localhost:8000
- API Docs: http://localhost:8000/docs
- Celery Flower: http://localhost:5555

### Makefile Commands

| Command | Description |
|---------|-------------|
| `make build` | Build Docker images |
| `make run` | Start in detached mode |
| `make up` | Start in foreground |
| `make stop` | Stop containers |
| `make down` | Remove containers |
| `make test` | Run backend tests |
| `make migrate` | Apply database migrations |
| `make create_migration m="..."` | Create new migration |
| `make seed` | Seed sample data |

### Code Quality

**Backend:**
```bash
cd backend && uv run pre-commit run --all-files
```

**Frontend:**
```bash
cd frontend && pnpm run lint:fix && pnpm run format
```

### Database Migrations

When you rebase and `main` gained a migration in the meantime, `alembic heads` shows two heads. Resolve it by moving **your** migration to the end of the chain. Never touch a migration that is already on `main`: databases that applied it treat everything inserted before it as already done and skip it silently.

1. Set `down_revision` of your migration to the head from `main`.
2. Rename your file so its date is later than the last migration on `main`. Keep the `rev` id. If your dev database already ran this migration, run `make downgrade` before re-pointing and `make migrate` after; otherwise the database keeps your revision as current and never applies the migration from `main`.
3. CI checks the chain and fails on a second head, on a changed `down_revision` in a migration already on `main`, and on a deleted or renamed migration.

## This is a Fork

Upstream is [`the-momentum/open-wearables`](https://github.com/the-momentum/open-wearables).
This fork carries a large, long-lived delta and re-syncs periodically, so when
something misbehaves the first question is whether the code is ours or theirs.

```bash
make fork-setup   # once per clone: adds the 'upstream' remote and fetches it
make fork-diff    # regenerate docs/fork/DIVERGENCE.md
make fork-tag     # after merging upstream: tag HEAD as upstream-sync/<date>
make fork-stamp   # refresh frontend/fork-version.json (v=0.2.0 to bump the fork version)
```

The sidebar footer shows both versions, each with the date its code last moved:

```
OW    v0.9.0 · 4aa4dbf      # upstream's release, and the newest upstream commit merged in
      2026-09-18 11:59 UTC
fork  v0.1.0 · 15fbc66      # this fork's version, and the commit built from
      2026-09-21 17:37 UTC
```

Upstream's version comes from `frontend/package.json` (synced from upstream — do not
edit it) and its date from the merge base with `upstream/main`, so two syncs a month
apart are distinguishable even though both are "v0.9.0".

Each value is resolved at build time from the first source that has it:

| | Deployed image (`build.yml` → GHCR) | Host build / `pnpm dev` | Local `make build` |
|---|---|---|---|
| fork commit + date | CI build args, from the pushed commit | live git | the stamp |
| fork version | the stamp | the stamp | the stamp |
| upstream commit + date | the stamp | live git (merge base) | the stamp |

The stamp is `frontend/fork-version.json`, committed because the frontend image is
built from a `./frontend` context with no `.git` in it. **The fork's commit and date
must not depend on it in a deployed image** — the stamp only moves when someone runs
`make fork-stamp`, so an image that read it would freeze at whenever that last
happened, which is what `build.yml` passing `FORK_COMMIT`/`FORK_UPDATED_AT` prevents.

What the stamp still owns is the fork version (bump it with `make fork-stamp v=<semver>`)
and the upstream sync point, both of which change only when a person changes them.
**Run `make fork-stamp` as part of every upstream merge**, alongside `make fork-diff`
and `make fork-tag`; it needs the `upstream` remote (`make fork-setup`) and keeps the
previous value without it.

The tooltip on the footer also carries the image's build time. If that is recent but
the fork date is old, the code genuinely has not changed; if the build time is old, the
deployment is serving a stale image and has not pulled.

- **[docs/fork/DIVERGENCE.md](docs/fork/DIVERGENCE.md)** - generated. Every file that
  differs from upstream, marked fork-only or modified. A file that is *not* listed is
  identical to upstream, so a bug in it is upstream's and is worth reproducing against
  a clean upstream checkout first.
- **[docs/fork/DECISIONS.md](docs/fork/DECISIONS.md)** - hand-written. Why each
  divergence exists and, on a sync conflict, whether to keep ours or take theirs.

When you change a file that also exists upstream, add a DECISIONS.md entry. When you
merge upstream, run `make fork-diff`, `make fork-tag` and `make fork-stamp`, and commit
all three.
The `upstream-sync/<date>` tags are the bisect anchors: `git diff upstream-sync/<date>..HEAD`
is exactly "what have we changed since a known-good base".

## Guidelines for AI Agents

1. **Read specialized docs** - See `backend/AGENTS.md` and `frontend/AGENTS.md` for patterns
2. **Never commit secrets** - Check for .env files, API keys, credentials
3. **Follow existing patterns** - Match the code style of surrounding files
4. **Run quality checks** - Always run lint/format after changes
5. **Use type hints** - All Python functions must have type annotations
6. **Test your changes** - Run relevant tests before considering work complete
7. **Update documentation** - When adding or changing endpoints, providers, integration logic, API contracts, or features, update the relevant pages in `docs/`
8. **Update API Reference navigation** - When adding, removing, or renaming **external** API endpoints (tagged `External: *`), update the `API Reference` tab in `docs/docs.json` to keep the endpoint list in sync
9. **Think beyond the current feature** - Before adding provider-specific or otherwise narrow logic, ask whether the same behavior will be needed elsewhere (other providers, entities). If likely, propose a shared abstraction upfront instead of a one-off implementation.

## Documentation Standards (docs/)

When working on documentation in the `docs/` directory:

### Code Examples
- Include complete, runnable examples users can copy and execute
- Show proper error handling and edge case management
- Use realistic data instead of placeholder values
- Include expected outputs for verification
- Specify language and include filename when relevant
- Never include real API keys or secrets

### API Documentation
- Document all parameters including optional ones with clear descriptions
- Show both success and error response examples with realistic data
- Include rate limiting information with specific limits
- Provide authentication examples showing proper format
- Explain all HTTP status codes and error handling

### Accessibility
- Include descriptive alt text for all images and diagrams
- Use specific, actionable link text instead of "click here"
- Ensure proper heading hierarchy starting with H2
- Structure content for easy scanning with headers and lists

### Mintlify Component Selection
- **Steps** - For procedures and sequential instructions
- **Tabs** - For platform-specific content or alternative approaches
- **CodeGroup** - For showing same concept in multiple programming languages
- **Accordions** - For progressive disclosure of information
- **RequestExample/ResponseExample** - For API endpoint documentation
- **ParamField** - For API parameters, **ResponseField** - For API responses
- **Expandable** - For nested object properties or hierarchical information
