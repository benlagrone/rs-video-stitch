# MediaStudio on Fortress Sextant

The complete MediaStudio runtime belongs on `fortress.sextant`, including the
generic project desk, real-estate video tooling, and Bible Video Studio. They
share one API, worker, project store, render pipeline, thumbnail generator, and
reviewed YouTube integration. Phronesis remains a protected model-serving host;
it does not own MediaStudio workflow or project state.

MediaStudio also owns the no-attribution sound-effects catalog. Codex or a
Sextant-local MCP client imports individually downloaded Pixabay or Mixkit
audio through `mcp/sfx_mcp.py`; the MCP rejects unknown providers and any item
that requires attribution or disallows commercial/social use. The Projects
control surface reads the catalog through the same-origin, read-only
`/v1/sfx/catalog` route and lists approved inventory without exposing server
file paths. An empty catalog is healthy and means no approved sounds have been
imported yet.

Bible Video Studio is a server workflow, not a laptop CLI. Its locked runtime
host is `fortress.sextant`. The browser calls the MediaStudio same-origin API;
the API queues the complete passage-to-video job, and the Sextant worker calls
the private Fortress model-serving endpoints over Tailscale server-side. ComfyUI workflow
submission, polling, artifact download, quality gates, rendering, project state,
and publishing logic all execute on Sextant.

```text
browser -> Sextant MediaStudio API -> Sextant video worker
                                      -> Fortress LAN image GPU
                                      -> Fortress LAN ComfyUI/Wan model
                                      -> Fortress LAN voice gateway
                                      -> YouTube API after explicit review
```

The UI supports `Still` and `Motion`. Both modes retrieve the named passage,
create one audited scene per verse, generate narration, render a 1080p MP4,
and keep YouTube publishing behind a separate confirmation dialog.

## Locked runtime

- Host: `fortress.sextant` (`192.168.0.36` observed on 2026-08-10)
- Compose project: `mediastudio-sextant`
- Compose file: `docker-compose.yml`
- Network: `mediastudio-sextant-net`
- Private UI: `http://fortress-sextant.lan:8082/media-studio`
- Storage: `~/Videos/MediaStudio` for Bible, real-estate, and generic projects
- SFX library: `~/Videos/MediaStudio/sfx-library`
- SFX MCP: `python3 mcp/sfx_mcp.py` with `SFX_LIBRARY_ROOT` set to that library

The host must pass the Colima/Docker preflight before deployment. Stable
Diffusion, Ollama, Wan/ComfyUI, and the central voice gateway are reached at
the Phronesis Tailscale address (`100.100.97.30`), which preserves the model
firewall instead of opening model ports to the general LAN. Voice provider
tokens and provider-selection policy stay inside the Fortress voice gateway;
YouTube credentials stay in the Sextant server environment. No credential is
returned to the browser.

MediaStudio keeps independent `english` and `mandarin` YouTube publishing
profiles. The existing `/videos/youtube_token.json` remains the English token;
Mandarin authorization is stored separately at
`/videos/youtube_token_mandarin.json`. Chinese-language real-estate projects
default to the Mandarin profile, while the final upload remains an explicit
review action. Connecting the Mandarin profile requires the user to choose the
intended Google account and channel in Google's OAuth flow once.

The installed-app OAuth client returns to
`http://localhost:8082/v1/youtube/auth/callback`. On Benjamin's Mac, the Data
Fabric Inventory nginx frontend owns that loopback port and forwards only this
exact callback route to Sextant MediaStudio. All other localhost port 8082
routes remain Data Fabric routes. With `YOUTUBE_LOOPBACK_BRIDGE=true`,
MediaStudio polls for the completed server token and does not ask the user to
copy and paste the callback URL. Google may still require its own one-time
unverified-app acknowledgement until the OAuth consent app is verified.

Sextant keeps Colima's global `portForwarder` disabled. The launchd service
`com.fortress.mediastudio-relay` publishes only locked port `8082` through
Colima's authenticated SSH transport. This preserves fail-closed containers
while making the declared private LAN UI reachable.

## Smoke check

Run from the checked-out repository on Sextant after deployment:

```bash
bash scripts/verify-sextant-mediastudio.sh
bash scripts/verify-sextant-bible-video.sh
```

The full MediaStudio smoke check verifies placement, API readiness, UI
availability, the no-attribution SFX inventory contract, migrated real-estate
project assets and outputs, and YouTube authorization. The Bible smoke check
separately verifies image and motion provider health.

## Full-project-store migration evidence

Validated on 2026-08-10:

- The stopped Phronesis project store was staged byte-for-byte on Sextant:
  32 project directories, 2,647 files, and 3,616,225,327 bytes.
- Sextant's existing Bible store was backed up before merging the staged
  projects; the Phronesis source was retained unchanged for rollback.
- The restarted Sextant runtime reported 35 projects: three Bible projects and
  21 JHNG01 real-estate projects, plus other generic MediaStudio projects.
- A migrated English real-estate MP4 validated as 1920x1080 H.264 with audio;
  its generated thumbnail validated as a 1280x720 JPEG.
- Sextant retained the newer YouTube credential and reported authenticated for
  the upload scope.

## Live acceptance evidence

Validated on 2026-08-10 from commit `d7cd292`:

- Still: `John 3:16`, completed as a 1920x1080 H.264/AAC MP4.
- Motion: `Psalm 23:1`, completed with an 81-frame, 5.06-second Wan source clip
  and a 1920x1080 H.264/AAC final MP4. Early and late frames were visually
  inspected and confirmed actual motion.
- The former `fortress-lan-mediastudio-1` application container was stopped
  after both acceptance runs. Phronesis model and voice containers remained
  running.
- The existing YouTube OAuth client and token were migrated server-to-server;
  Sextant reports `authenticated: true` for the upload scope. No acceptance
  video was uploaded. The upload-only scope does not permit a read-only channel
  identity lookup, so the UI's explicit final publishing review remains the
  channel confirmation gate.
