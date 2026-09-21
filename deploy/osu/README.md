# Open Wearables on OTDI Container Services

Kubernetes manifests for running Open Wearables on Ohio State's [OTDI Container
Services](https://it.osu.edu/otdi-container-services) (managed Kubernetes).
The narrative guide, including the OSU-specific approval steps that have to
happen first, is in [`docs/deployment/osu-container-services.mdx`](../../docs/deployment/osu-container-services.mdx).

These manifests have not been applied against an OTDI cluster. The values
marked `REPLACE-...` and the three questions in `ingress.yaml` need answers
from OTDI before the first `kubectl apply`.

## Layout

| File | What it creates |
|------|-----------------|
| `configmap.yaml` | Non-secret runtime config (`ow-config`) |
| `redis.yaml` | Redis StatefulSet + headless Service, 5Gi PVC |
| `api.yaml` | FastAPI Deployment (1 replica, runs migrations on start) + Service |
| `worker.yaml` | Celery worker Deployment |
| `beat.yaml` | Celery beat Deployment (1 replica, always) |
| `frontend.yaml` | Frontend Deployment + Service |
| `ingress.yaml` | Ingress for the portal and API hostnames |
| `kustomization.yaml` | Ties the above together, pins image tags |

PostgreSQL is **not** in here: use the managed PostgreSQL that comes with the
service and point `DB_HOST`/`DB_NAME`/`DB_USER` at it.

## Secrets

Never commit these. Build a `secrets.env` from
`backend/config/.env.example` containing only the sensitive keys:

```
DB_PASSWORD=...
SECRET_KEY=...
ADMIN_PASSWORD=...
RESEND_API_KEY=...
OPEN_WEARABLES_API_KEY=...
GARMIN_CLIENT_ID=...
GARMIN_CLIENT_SECRET=...
# ...one pair per provider you enable
```

Then:

```bash
kubectl create secret generic ow-secrets --from-env-file=secrets.env
```

Rotating a value is `kubectl create secret ... --dry-run=client -o yaml |
kubectl apply -f -` followed by `kubectl rollout restart deploy/ow-api
deploy/ow-worker deploy/ow-beat`.

## Deploy

```bash
kubectl apply -k deploy/osu
kubectl rollout status deploy/ow-api
```

First rollout takes a few minutes: `scripts/start/app.sh` applies Alembic
migrations and the seed scripts before the API listens, which is why the API
pod has a 5-minute `startupProbe` budget.

## Known constraints

- **API replicas stay at 1.** The start script runs migrations and seeding.
  To scale out, move those into a pre-rollout Job and start FastAPI directly.
- **Beat replicas stay at 1.** Two schedulers means every sync fires twice.
- **Non-root.** Both images ship without a `USER`, so they default to root.
  The manifests force uid 1001, redirect `HOME` and `UV_CACHE_DIR` to a `/tmp`
  emptyDir, and override the beat command so its pidfile and schedule land in
  `/tmp`. If OTDI's namespace policy requires a different uid range, change
  `runAsUser`/`fsGroup` in all four workloads to match.
- **Flower is not deployed.** It has no authentication of its own; if you want
  it, put it behind Shibboleth rather than on a public ingress.
- **Svix is not deployed**, so `OUTGOING_WEBHOOKS_ENABLED` is `false`. Enabling
  it needs a second database named `svix` and a `svix-server` Deployment.
