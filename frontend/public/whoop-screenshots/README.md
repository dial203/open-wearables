# WHOOP review screenshots

Captures referenced by `../whoop-integration.html`, the page linked from the
WHOOP Developer Platform access request as the UX/screenshots link.

Drop PNGs in here with these exact names. Any file that is missing renders as a
labelled placeholder on the page, so the page stays readable while captures are
still being collected.

| File | What to capture | Status |
| --- | --- | --- |
| `01-connect.png` | Provider connection screen with WHOOP listed | pending |
| `02-authorize.png` | WHOOP's own consent screen showing the requested scopes | pending |
| `03-connection-status.png` | Connection management view: active WHOOP connection, last sync, disconnect control | pending |
| `04-compare-sources.webp` | One night's sleep metrics from WHOOP next to every other source | done |
| `05-device-landscape.png` | Accuracy vs trend fidelity across connected devices | done |
| `06-whoop-agreement.png` | WHOOP 5.0 heart rate vs reference standard, scatter + Bland-Altman | done |

Before capturing:

- Use your own WHOOP account or a demo user. No participant names, emails or
  user IDs may be visible — blur anything identifying.
- WHOOP must be visibly attributed as the source in every view that displays
  WHOOP-derived metrics.
- Capture at a consistent window width so the page reads as one set.

The page is served at `/whoop-integration.html` (Nitro serves `frontend/public/`
at the site root) and carries `noindex, nofollow`.
