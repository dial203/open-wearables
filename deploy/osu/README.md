# Open Wearables on OTDI Container Services

Manifests for running Open Wearables on Ohio State's
[Container Services](https://it.osu.edu/otdi-container-services) platform.
They follow that platform's conventions, which differ from a generic
Kubernetes deployment in four ways that matter:

- **GitOps.** You never `kubectl apply`. Commit YAML to your Flux repository at
  `https://repo.service.osu.edu/ContainerService/customer_flux/flux_<namespace>_cs`
  under `production/`, add the filename to `production/kustomization.yaml`, and
  a CI/CD tool reconciles it. The repo is reachable only from OSU IP space.
- **Istio, not Ingress.** Routing is a `VirtualService` + `DestinationRule`
  bound to `istio-system/istio-gateway` (public) or
  `istio-system/istio-internal-gateway` (OSU address space only).
- **Apache sidecar.** Every web-facing pod runs
  `registry.containers.it.osu.edu/ocio/docker-apacheshib-revproxy` on `:8080`,
  proxying to the app on `localhost`. It terminates the Istio hop and is where
  Shibboleth SSO would be configured.
- **No root.** The cluster rejects root containers. Both Open Wearables images
  ship without a `USER`, so every app container here sets `runAsUser`
  explicitly.

The guide with the university-side process — data classification, risk
assessment, requesting the service — is in
[`docs/deployment/osu-container-services.mdx`](../../docs/deployment/osu-container-services.mdx).

## Files

| File | Objects |
|------|---------|
| `config.yaml` | `ow-config` ConfigMap — shared non-secret env |
| `secrets.example.yaml` | Template for `ow-secrets`. **Read the warning in it.** |
| `redis.yaml` | Celery broker, ephemeral storage |
| `api.yaml` | FastAPI + Apache sidecar, Service, VirtualService, DestinationRule |
| `frontend.yaml` | Portal + Apache sidecar, Service, VirtualService, DestinationRule |
| `workers.yaml` | Celery worker and beat (no inbound traffic) |
| `kustomization.yaml` | Reference copy; the Flux repo's `production/kustomization.yaml` is authoritative |

PostgreSQL is not here. Use the managed Postgres that comes with the service
and point `DB_HOST` at it.

## Placeholders

Replace throughout before committing:

| Token | Value |
|-------|-------|
| `PLACEHOLDER_NAMESPACE` | Namespace assigned by Container Services |
| `PLACEHOLDER_PORTAL_DNS` | Portal hostname, e.g. `wearables.webtest.osu.edu` |
| `PLACEHOLDER_API_DNS` | API hostname, e.g. `wearables-api.webtest.osu.edu` |
| `PLACEHOLDER_DB_HOST` | Managed Postgres hostname |
| `PLACEHOLDER_EMAIL` | Technical contact, OSU address |
| `PLACEHOLDER_REGISTRY` | e.g. `registry.containers.it.osu.edu/<your-project>` |

## Images

Mirror the published images into the OSU registry rather than pulling from
Docker Hub — cluster egress to Docker Hub is not guaranteed, and pinning a
digest in your own registry makes the deployment reproducible:

```bash
skopeo copy docker://themomentum/open-wearables-backend:0.7.0 \
  docker://registry.containers.it.osu.edu/<project>/open-wearables-backend:0.7.0
skopeo copy docker://themomentum/open-wearables-frontend:0.7.0 \
  docker://registry.containers.it.osu.edu/<project>/open-wearables-frontend:0.7.0
```

The pull secret `<namespace>-registry-pull` is pre-provisioned in your
namespace and referenced by every Deployment here.

> **The pull secret token expires 09-2026.** Contact Container Services to
> renew it, or images stop pulling and any pod restart fails.

## Access model

This is the decision the risk assessment will focus on, so it is worth being
precise about what protects what.

The API host serves every browser and SDK call, plus provider OAuth callbacks
(`/api/v1/oauth/*`) and inbound provider webhooks (`/api/v1/*/webhooks`).
**None of those can sit behind Shibboleth** — an XHR redirected to the IdP
fails, and provider webhook calls carry no OSU identity. So the API host is
either public, protected only by Open Wearables' own bearer tokens, or
restricted to OSU address space, in which case participants can enroll and
sync only from campus or VPN.

Open Wearables has no MFA of its own. If the deployment holds S4 data and the
control requirement is MFA, neither option satisfies it at the application
layer on its own — restricting both hosts to the internal gateway and relying
on VPN MFA is the configuration that comes closest, at the cost of off-campus
participants. Raise this with `securemyresearch@osu.edu` rather than assuming
the Apache sidecar solves it.

Shibboleth is off on the portal too, because
`frontend/src/routes/accept-invite.tsx` and `widget.connect.tsx` are the
participant enrollment and provider-connect flows — SSO there locks out anyone
without an OSU ID.

### Enabling Shibboleth

Only if your cohort is entirely OSU-affiliated. In `frontend.yaml`, replace the
Apache container's `env` with:

```yaml
- name: APACHESHIB_ENTITYID
  value: https://PLACEHOLDER_PORTAL_DNS/shibboleth
- name: APACHESHIB_HOST
  value: PLACEHOLDER_PORTAL_DNS
- name: APACHESHIB_EMAIL
  value: PLACEHOLDER_EMAIL
- name: APACHESHIB_PORT_HTTP_TYPE
  value: revproxy
- name: APACHESHIB_TARGET
  value: http://localhost:3000
- name: APACHESHIB_PROXY_FLAGS
  value: ping=3 keepalive=On disablereuse=On
```

and add to `local.conf` in the ConfigMap:

```
<Location />
    AuthType shibboleth
    ShibRequestSetting requireSession 1
    ShibRequestSetting redirectToSSL 443
    ShibRequestSetting authnContextClassRef urn:mace:osu.edu:shibboleth:ac:classes:mfa
    ShibUseHeaders On
    Require shib-session
</Location>
```

Then generate SP keys, add `sp-cert.pem` / `sp-key.pem` to the ConfigMap, mount
them at `/etc/shibboleth/pki/`, and register the cert with OSU Identity and
Access Management:

```bash
docker run -it --entrypoint=/bin/bash \
  registry.containers.it.osu.edu/ocio/docker-apacheshib-revproxy
# inside: cd /etc/shibboleth && keygen.sh
```

## Deploying

1. Replace every placeholder.
2. Resolve the secrets question (see `secrets.example.yaml`) — do not commit
   base64 credentials to the Flux repo without asking Container Services what
   they support.
3. Copy `config.yaml`, `redis.yaml`, `api.yaml`, `frontend.yaml`,
   `workers.yaml` into `production/` in the Flux repo.
4. Add each filename to `production/kustomization.yaml`.
5. Commit, or open a merge request if the repo requires approval.
6. Watch the pods at <https://containers.it.osu.edu/> — select your namespace.

First reconcile takes several minutes: `scripts/start/app.sh` applies Alembic
migrations and seed scripts before the API listens, which is why `ow-api` has a
5-minute `startupProbe` budget.

## Billing

$20 per unit per month. One unit is the **highest** of 1 GB RAM, 3 vCPU, or
10 GB persistent storage — you are billed on the binding dimension, not the
sum, so the others have free headroom until they overtake it.

As configured here the pods request **1.69 GB RAM and 0.75 vCPU**. RAM binds,
so that is 2 units — roughly **$40/month** — and database storage costs nothing
extra until it passes about **17 GB**, at which point storage binds instead and
every further 10 GB adds a unit.

Cost levers, roughly in order of size:

| Lever | Effect |
|-------|--------|
| A separate dev namespace | Doubles compute. Run prod only, or confirm whether an idle namespace still bills |
| `DEFAULT_DATA_GRANULARITY` | `raw` (the default) is what validation work needs, and is the main driver of table growth |
| `INGEST_WORKOUT_SAMPLES`, `STORE_FIT_FILES`, `RAW_PAYLOAD_STORAGE` | All off by default. Each one materially increases storage — leave them off unless you need them |
| Data lifecycle / archival settings | Retention policy is the lasting fix once tables grow; see the developer portal settings |
| `ow-frontend` replicas | Each replica is 320 Mi of the RAM total |
| Consulting time | $20 per 15 minutes, banked — about $80/hour. Arrive at meetings with specific questions |

Confirm with Container Services whether metering is on resource requests or
actual usage. If it is actual usage, the idle footprint is lower than the
requests above and a dev namespace that sits stopped may cost little.

Team access and usage: Team access and usage: <https://containerbilling.org.ohio-state.edu/>

## Known constraints

- **`ow-api` stays at 1 replica.** The start script runs migrations and
  seeding. To scale out, move those to a Job and start FastAPI directly.
- **`ow-beat` stays at 1 replica.** Two schedulers double every sync.
- **Redis is ephemeral.** See the note in `redis.yaml`.
- **Flower is not deployed.** It has no authentication of its own.
- **Svix is not deployed**, so `OUTGOING_WEBHOOKS_ENABLED` is `false`.
  Enabling it needs a second managed database named `svix`.
- **`runAsUser` values are unverified against the images** — 1001 for the
  backend (arbitrary non-root; the image has no `USER`), 1000 for the frontend
  (`node` in `node:22-alpine`). If a pod crashes on a permissions error, this is
  the first thing to check.
