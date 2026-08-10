# Bible Video Studio on Fortress Sextant

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
- Private UI: `http://fortress-sextant.local:8082/media-studio`
- Storage: `~/Videos/MediaStudio`

The host must pass the Colima/Docker preflight before deployment. Stable
Diffusion, Ollama, Wan/ComfyUI, and the central voice gateway are reached at
the Phronesis Tailscale address (`100.100.97.30`), which preserves the model
firewall instead of opening model ports to the general LAN. Voice provider
tokens and provider-selection policy stay inside the Fortress voice gateway;
YouTube credentials stay in the Sextant server environment. No credential is
returned to the browser.

Sextant keeps Colima's global `portForwarder` disabled. The launchd service
`com.fortress.mediastudio-relay` publishes only locked port `8082` through
Colima's authenticated SSH transport. This preserves fail-closed containers
while making the declared private LAN UI reachable.

## Smoke check

Run from the checked-out repository on Sextant after deployment:

```bash
bash scripts/verify-sextant-bible-video.sh
```

The smoke check verifies placement, API readiness, UI availability, and the
reported health of MediaStudio, the image GPU, and the motion GPU.

## Live acceptance evidence

Validated on 2026-08-10 from commit `d7cd292`:

- Still: `John 3:16`, completed as a 1920x1080 H.264/AAC MP4.
- Motion: `Psalm 23:1`, completed with an 81-frame, 5.06-second Wan source clip
  and a 1920x1080 H.264/AAC final MP4. Early and late frames were visually
  inspected and confirmed actual motion.
- The former `fortress-lan-mediastudio-1` application container was stopped
  after both acceptance runs. Phronesis model and voice containers remained
  running.
