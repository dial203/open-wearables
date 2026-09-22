# WHOOP review screenshots

Captures referenced by `../whoop-integration.html`, the page linked from the
WHOOP Developer Platform access request as the UX/screenshots link.

All seven are in place. Any file that goes missing renders as a labelled
placeholder on the page rather than a broken image.

| File | What it shows |
| --- | --- |
| `01-connect.png` | Provider connection screen with WHOOP among the supported platforms |
| `02-link-account.png` | Which WHOOP account is being linked and what it is for, before the redirect |
| `03-authorize.png` | WHOOP's own consent screen listing the requested scopes |
| `04-connected.png` | Connection confirmed, active and syncing |
| `05-compare-sources.webp` | One night's sleep metrics from WHOOP next to every other source |
| `06-whoop-agreement.png` | WHOOP 5.0 vs Polar H10: pooled agreement and night-by-night error, no per-member rows |
| `07-device-landscape.png` | Accuracy vs trend fidelity across connected devices |

01-04 were rendered from saved copies of the live pages. To recapture:

- Use your own WHOOP account or a demo user. No participant names, initials,
  emails or user IDs may be visible. `06-whoop-agreement.png` was captured with
  the per-participant table hidden and the remaining labels relabelled P01-P04;
  keep both for any recapture.
- WHOOP must be visibly attributed as the source in every view that displays
  WHOOP-derived metrics.
- Capture at a consistent window width so the page reads as one set.

The page is served at `/whoop-integration.html` (Nitro serves `frontend/public/`
at the site root) and carries `noindex, nofollow`.
