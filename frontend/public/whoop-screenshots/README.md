# WHOOP review screenshots

Captures referenced by `../whoop-integration.html`, the page linked from the
WHOOP Developer Platform access request as the UX/screenshots link.

Drop PNGs in here with these exact names. Any file that is missing renders as a
labelled placeholder on the page, so the page stays readable while captures are
still being collected.

| File | What to capture |
| --- | --- |
| `01-connect.png` | Provider connection screen with WHOOP listed |
| `02-authorize.png` | WHOOP's own consent screen showing the requested scopes |
| `03-connection-status.png` | Connection management view: active WHOOP connection, last sync, disconnect control |
| `04-recovery.png` | Dashboard with WHOOP recovery score, HRV, resting heart rate |
| `05-sleep.png` | Sleep detail: duration, stage totals, sleep performance |
| `06-workout.png` | A WHOOP workout with type, duration, strain, heart-rate summary |
| `07-cross-device.png` | Same night or session from WHOOP next to another device |

Before capturing:

- Use your own WHOOP account or a demo user. No participant names, emails or
  user IDs may be visible — blur anything identifying.
- WHOOP must be visibly attributed as the source in every view that displays
  WHOOP-derived metrics.
- Capture at a consistent window width so the page reads as one set.

The page is served at `/whoop-integration.html` (Nitro serves `frontend/public/`
at the site root) and carries `noindex, nofollow`.
