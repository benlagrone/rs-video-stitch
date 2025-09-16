# Render API

Headless FastAPI service plus worker that renders narrated slideshows into 1080p MP4 files using FFmpeg. The API accepts scene specifications, assets, and render options, queues jobs in SQLite, and a companion worker container pulls jobs and produces artifacts on a shared `/data` volume.

## Architecture

- **render-api service** – FastAPI app handling authentication, project + asset management, render job creation, job status, and artifact streaming.
- **render-worker service** – Python worker loop that polls queued jobs, runs the FFmpeg pipeline, emits progress/logs, and records artifacts.
- **Shared storage** – Docker volume mounted at `/data` inside both containers for SQLite, logs, input, work, and output media.

```
render-api/
  app/
    api.py         # FastAPI routes
    auth.py        # Bearer auth dependency
    db.py          # SQLAlchemy session + bootstrap
    models.py      # Project/Job/Artifact tables
    renderer.py    # FFmpeg-based render pipeline
    schemas.py     # Pydantic request contracts
    storage.py     # Helpers for /data layout
    worker.py      # Polls jobs and renders outputs
  Dockerfile

docker-compose.yml
README.md
```

## Storage Layout

Mounted host directory `./data` is used as `/data` inside containers:

```
/data/
  projects/<projectId>/
    input/
      scenes.json
      images/...
      voiceovers/...
    work/                  # temp intermediates (kept on failure)
    output/
      video.mp4
      scene_00.mp4 ...
  db.sqlite3
  logs/
```

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `RENDER_STORAGE` | `/data` | Root for shared storage volume. |
| `DB_URL` | `sqlite:////data/db.sqlite3` | SQLAlchemy connection string. |
| `AUTH_TOKEN` | `change-me` | Bearer token required by all API routes. |
| `ALLOW_ORIGINS` | `http://localhost:5173` | Comma-delimited origins allowed by CORS. |
| `DEFAULT_FPS` | `30` | Default frames per second if request omits it. |
| `DEFAULT_MIN_SHOT` | `2.5` | Minimum per-image duration in seconds. |
| `DEFAULT_MAX_SHOT` | `8.0` | Maximum per-image duration in seconds. |
| `DEFAULT_XFADE` | `0.5` | Default cross-fade length in seconds. |
| `DEFAULT_CRF` | `18` | Default H.264 CRF quality. |
| `DEFAULT_PRESET` | `medium` | Default encoder preset. |

## Running with Docker Compose

1. Ensure Docker Desktop or compatible engine is available.
2. Place your project inputs under `data/projects/<projectId>/input/` or use the API.
3. Build and launch:

   ```bash
   docker compose up -d --build
   ```

4. API is available on `http://localhost:8080` by default. Worker container shares the same image and consumes render jobs automatically.

To stop the stack:

```bash
docker compose down
```

## API Endpoints

All endpoints require `Authorization: Bearer <AUTH_TOKEN>`.

| Method & Path | Description |
| --- | --- |
| `GET /healthz`, `GET /readyz` | Basic liveness/readiness checks. |
| `PUT /v1/projects/{id}/scenes` | Upsert full project spec (validated, ≤3 images/scene) and write `scenes.json`. |
| `POST /v1/projects/{id}/assets` | Multipart upload for asset files; optional `subdir` of `images` or `voiceovers`. |
| `POST /v1/projects/{id}/render` | Queue a render job with output name + render options; returns `jobId`. |
| `GET /v1/jobs/{jobId}` | Poll job status, progress (0..1), stage, error, and recent logs. |
| `GET /v1/projects/{id}/outputs` | List generated files under `output/`. |
| `GET /v1/projects/{id}/outputs/video` | Stream/download the final MP4 (defaults to last output name or `video.mp4`). |

## Render Options Schema

```json
{
  "outputName": "video.mp4",
  "renderOptions": {
    "fps": 30,
    "minShot": 2.5,
    "maxShot": 8.0,
    "xfade": 0.5,
    "crf": 18,
    "preset": "medium",
    "tts": null,
    "voiceDir": "voiceovers",
    "music": null,
    "ducking": false
  }
}
```

## Job Lifecycle

`QUEUED → RUNNING → (SUCCEEDED | FAILED | CANCELLED)` with stages typically stepping through `VALIDATE`, `AUDIO_PREP`, `SCENE_BUILD[n]`, `CONCAT`, `FINALIZE`. The worker writes progress updates into the database and streams detailed logs to `/data/logs/<jobId>.log`, which the API tails for the status endpoint.

## Smoke Test

Replace placeholders with your host/IP, project ID, token, and asset paths.

```bash
TOKEN=change-me
PID=p_example
BASE=http://localhost:8080

curl -sS -X PUT "$BASE/v1/projects/$PID/scenes" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data-binary @data/scenes.json

curl -sS -X POST "$BASE/v1/projects/$PID/assets" \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@assets/images/sample.jpg" \
  -F "subdir=images"

JOB=$(curl -sS -X POST "$BASE/v1/projects/$PID/render" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"outputName":"demo.mp4","renderOptions":{"fps":30,"minShot":2.5,"maxShot":8.0,"xfade":0.5,"crf":18,"preset":"medium","tts":null,"voiceDir":"voiceovers","music":null,"ducking":false}}' | jq -r '.jobId')

echo "Queued job: $JOB"

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/v1/jobs/$JOB"

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/v1/projects/$PID/outputs/video" -o dist/demo.mp4
```

## Development Notes

- The Docker image installs `ffmpeg`, `jq`, and `tini`; additional build deps can be added in `render-api/Dockerfile` if needed.
- `render-api/app/renderer.py` is the FFmpeg orchestration point; extend it for music beds, ducking, or additional effects.
- Logs and intermediates remain in `/data/projects/<projectId>/work` on failure for debugging. Successful runs leave outputs in `/output` and trace logs in `/data/logs`.
- Rotate `AUTH_TOKEN`, restrict network exposure, and front with a reverse proxy if deploying beyond your LAN.

## License

This repository currently has no explicit license. Add one if you plan to distribute or share the project.
