# Render API

Headless FastAPI service plus worker that renders narrated slideshows into 1080p MP4 files using FFmpeg. The API accepts scene specifications, assets, and render options, queues jobs in SQLite, and a companion worker container pulls jobs and produces artifacts on a shared `/videos` volume.

Each scene’s `title` is burned into the video frame using the bundled EB Garamond font so viewers see a consistent on-screen caption while that scene plays.

## Architecture

- **render-api service** – FastAPI app handling project + asset management, render job creation, job status, and artifact streaming.

- **render-worker service** – Python worker loop that polls queued jobs, runs the FFmpeg pipeline, emits progress/logs, and records artifacts. When the API runs outside Docker it can spawn the same worker in-process for convenience.

- **Shared storage** – Docker volume mounted at `/videos` inside both containers for SQLite, logs, input, work, and output media.

```
render-api/
  app/
    api.py         # FastAPI routes
    auth.py        # Bearer auth dependency
    db.py          # SQLAlchemy session + bootstrap
    models.py      # Project/Job/Artifact tables
    renderer.py    # FFmpeg-based render pipeline
    schemas.py     # Pydantic request contracts
    storage.py     # Helpers for /videos layout
    worker.py      # Polls jobs and renders outputs
  Dockerfile

docker-compose.yml
README.md
```

## Storage Layout

Mounted host directory `~/Videos` is mapped to `/videos` inside both containers:

```
~/Videos/
  db.sqlite3
  logs/
  projects/<projectId>/
    input/
      scenes.json
      images/...
      voiceovers/...
    work/                  # temp intermediates (kept on failure)
    output/
      video.mp4
      scene_00.mp4 ...
```

Rendered videos are written to `~/Videos/projects/<projectId>/output/` on the host. Final MP4s arrive alongside any per-scene intermediates the worker leaves behind for debugging.

Create the base directories before starting the stack:

```bash
mkdir -p ~/Videos/{logs,projects}
```

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `RENDER_STORAGE` | `/videos` | Root for shared storage volume inside the containers. When unset locally the API falls back to `~/Videos` (and then `./videos`) automatically. |
| `DB_URL` | `sqlite:////videos/db.sqlite3` | SQLAlchemy connection string. |
| `ALLOW_ORIGINS` | `http://localhost:5173` | Comma-delimited origins allowed by CORS. |
| `INLINE_WORKER` | `1` | When truthy, the FastAPI process launches a background worker thread. Set to `0` when running a dedicated worker process (Docker Compose already handles this). |
| `DEFAULT_FPS` | `30` | Default frames per second if request omits it. |
| `DEFAULT_MIN_SHOT` | `2.5` | Minimum per-image duration in seconds. |
| `DEFAULT_MAX_SHOT` | `8.0` | Maximum per-image duration in seconds. |
| `DEFAULT_XFADE` | `0.5` | Default cross-fade length in seconds. |
| `DEFAULT_CRF` | `18` | Default H.264 CRF quality. |
| `DEFAULT_PRESET` | `medium` | Default encoder preset. |
| `TITLE_FONT_FILE` | — | Override the TTF used for scene title overlays (defaults to `media/EB_Garamond/EBGaramond-VariableFont_wght.ttf`). |
| `XTTS_API_URL` | — | Base URL for xTTS HTTP endpoint (e.g. `http://xtts:5002`). |
| `XTTS_API_KEY` | — | Optional bearer token for the xTTS service. |
| `XTTS_LANGUAGE` | — | Optional language code passed to xTTS (default depends on service). |

### Optional xTTS Voice Synthesis

- Set `XTTS_API_URL` (and optionally `XTTS_API_KEY`, `XTTS_LANGUAGE`) to point at your xTTS server.
- Provide a `tts` value in `renderOptions` (for example the voice ID exposed by your service). Optionally include `ttsLanguage` if you need per-job overrides.
- When a scene lacks a matching audio file under `voiceDir`, the worker will call xTTS with the scene’s script and drop the generated WAV into the work directory.
- If xTTS is not configured or the request fails, the render job aborts so you know narration is missing.

### Project Spec JSON

`PUT /v1/projects/{projectId}/scenes` expects a payload that contains metadata, scene entries, and optional video-level defaults. Example:

```json
{
  "info": {
    "address": "9642 Meadowglen Lane, Houston, TX 77063"
  },
  "vid": {
    "voice": "p263",
    "lang": "en"
  },
  "scenes": [
    {
      "title": "Welcome / Intro",
      "VO": "Welcome to the property...",
      "images": ["front.jpg", "yard.jpg"]
    }
  ]
}
```

Fields under `vid` are optional. When present they become the default narration voice and language for subsequent renders (unless overridden in `renderOptions` or by explicit voiceover audio files).

## Running with Docker Compose

1. Ensure Docker Desktop or compatible engine is available.
2. Place your project inputs under `~/Videos/projects/<projectId>/input/` (create the directories if needed) or use the API to upload them.
3. Build and launch:

   ```bash
   docker compose down
   docker compose up -d --build
   ```

   Bringing the stack down first ensures any containers bound to the old port are removed before relaunch.

4. API is available on `http://192.168.86.23:8082` by default. Worker container shares the same image and consumes render jobs automatically. Interactive docs live at `http://192.168.86.23:8082/docs`.

To stop the stack:

```bash
docker compose down
```

### Streaming live logs while using Docker

- Follow the worker in real time with `docker compose logs -f render-worker` (swap `render-worker` for `render-api` to inspect the API service).
- Inspect a specific render job’s file from inside the container with `docker compose exec render-worker tail -f /videos/logs/<jobId>.log`.
- Because `/videos` is bind-mounted to `~/Videos` on the host, you can also run `tail -f ~/Videos/logs/<jobId>.log` without entering the container.


## Running Locally Without Docker

You can develop against the API and worker directly on your laptop without Docker. The steps below assume macOS/Linux, but they translate to Windows (PowerShell) with minor path syntax tweaks.

### Prerequisites

- Python **3.11** (matches the Docker image). Verify with `python3 --version`.
- `ffmpeg` installed and available on your `PATH`. On macOS: `brew install ffmpeg`. On Ubuntu/Debian: `sudo apt install ffmpeg`.
- A working directory on disk that can hold render inputs/outputs. The examples below use `~/Videos` to mirror the Docker volume.

### 1. Check out the repository

Clone or pull the latest code on your development machine and `cd` into the repo root:

```bash
git clone https://github.com/<your-org>/rs-video-stitch.git
cd rs-video-stitch
```

If you already have the repo, just `git pull` and `cd` into it.

### 2. Create the storage layout (optional)

The services will automatically create `~/Videos` (or `./videos`) the first time they run, but you can pre-create the structure if you prefer:

```bash
mkdir -p ~/Videos/{logs,projects}
```

Each project will create its own subdirectories under `~/Videos/projects/<projectId>/` as it runs.

### 3. Create and activate a virtual environment

All Python code lives under `render-api/`. Create a virtual environment there so `PYTHONPATH` lines up with module imports.

```bash
cd render-api
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r app/requirements.txt
```

You should now see `(.venv)` in your shell prompt. Keep this virtual environment active in any terminal that runs the API or worker.

### 4. Configure environment variables

Export the same settings the Docker containers rely on. Adjust the paths and TTS values to match your workstation:

```bash
export RENDER_STORAGE=${RENDER_STORAGE:-$HOME/Videos}
export XTTS_API_URL=${XTTS_API_URL:-http://xtts:5002}
export DB_URL=${DB_URL:-sqlite:////$RENDER_STORAGE/db.sqlite3}
export ALLOW_ORIGINS=${ALLOW_ORIGINS:-http://localhost:5173}
export PYTHONPATH=$(pwd)
```

Tips:

- Keep these exports in `render-api/.env.local` (or similar) and `source` it whenever you open a new terminal.
- `PYTHONPATH=$(pwd)` must reference the `render-api` directory so modules like `app.renderer` resolve correctly.
- The renderer falls back to voice `p263` and language `en` out of the box; override them by exporting `XTTS_VOICE` / `XTTS_LANGUAGE` only if you need different defaults. If your xTTS service requires authentication, also export `XTTS_API_KEY` before starting the API.

### 5. Start the FastAPI server (spawns worker automatically)

With the virtual environment active and variables exported:

```bash
uvicorn app.api:app --host 0.0.0.0 --port 8082 --reload
```

`--reload` enables auto-reloading when you edit files. Visit `http://localhost:8082/docs` to confirm the API is up. The server starts a background worker thread automatically (controlled by `INLINE_WORKER`, which defaults to `1` outside Docker).

### 6. (Optional) Run the worker separately

If you prefer to manage the worker manually—such as when benchmarking or running multiple workers—disable the inline thread with `export INLINE_WORKER=0` before launching Uvicorn. Then start the standalone worker in another terminal:

```bash
cd rs-video-stitch/render-api
source .venv/bin/activate
source ./env.local  # optional helper if you created one in step 4
export PYTHONPATH=$(pwd)
python -m app.worker
```

The standalone worker streams logs to `~/Videos/logs/<jobId>.log` while rendering. Tail the file for live updates with `tail -f ~/Videos/logs/<jobId>.log`.

### 7. Queue a test job (optional)

Once both processes are running you can exercise the pipeline with cURL:

```bash
BASE_URL=http://localhost:8082
PROJECT=p_local

# Upload scene spec
curl -X PUT "${BASE_URL}/v1/projects/${PROJECT}/scenes" \
  -H "Content-Type: application/json" \
  --data-binary @/path/to/scenes.json

# Upload image assets
curl -X POST "${BASE_URL}/v1/projects/${PROJECT}/assets?subdir=images" \
  -F "file=@/path/to/slide01.png"

# Kick off a render
curl -X POST "${BASE_URL}/v1/projects/${PROJECT}/render" \
  -H "Content-Type: application/json" \
  --data '{"outputName":"video.mp4","renderOptions":{}}'

# Poll status
curl "${BASE_URL}/v1/jobs/<jobId>"
```

Finished videos appear under `~/Videos/projects/${PROJECT}/output/`. Stop the API/worker with `Ctrl+C` when done.

## Companion xTTS Service (Optional)

If you want the renderer to synthesize narration automatically, stand up an xTTS server on the same Docker network and point `XTTS_API_URL` at it.

### 1. Scaffold a new project

```bash
mkdir -p ~/Projects/xtts-service
cd ~/Projects/xtts-service
```

Create `Dockerfile`:

```Dockerfile
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir TTS==0.22.0

ENV MODEL_NAME=tts_models/multilingual/multi-dataset/xtts_v2
ENV TTS_PORT=5002

EXPOSE ${TTS_PORT}

CMD ["tts-server", "--model_name", "${MODEL_NAME}", "--port", "${TTS_PORT}", "--use_cuda", "0", "--host", "0.0.0.0"]
```

Create `docker-compose.yml`:

```yaml
version: "3.9"
services:
  xtts:
    build: .
    image: local-xtts:latest
    container_name: xtts
    environment:
      - MODEL_NAME=${MODEL_NAME:-tts_models/multilingual/multi-dataset/xtts_v2}
      - TTS_PORT=${TTS_PORT:-5002}
    volumes:
      - ./cache:/root/.local/share/tts
    networks:
      - fortress-phronesis-net
    restart: unless-stopped
    ports:
      - "5002:5002"

networks:
  fortress-phronesis-net:
    external: true
```

First boot downloads the model into `./cache`, so keep that directory around for subsequent runs.

### 2. Launch xTTS

```bash
docker compose up -d --build
```

Once healthy, containers on `fortress-phronesis-net` can reach it at `http://xtts:5002/api/tts` (adjust the host/port if you expose it differently).

### 3. Point the render stack at xTTS

Back in your render project directory:

```bash
echo "XTTS_API_URL=http://xtts:5002" >> .env
# optional overrides
echo "XTTS_LANGUAGE=en" >> .env
```

Redeploy the render stack so it reads the updated `.env`:

```bash
docker compose down
docker compose up -d --build
```

Any render job that supplies `"tts": "${MODEL_NAME}"` (or another speaker ID supported by your server) now triggers automatic narration when no matching voiceover file exists.

## API Endpoints

All endpoints are currently unauthenticated.

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
    "ttsLanguage": null,
    "voiceDir": "voiceovers",
    "music": null,
    "ducking": false
  }
}
```

## Job Lifecycle

`QUEUED → RUNNING → (SUCCEEDED | FAILED | CANCELLED)` with stages typically stepping through `VALIDATE`, `AUDIO_PREP`, `SCENE_BUILD[n]`, `CONCAT`, `FINALIZE`. The worker writes progress updates into the database and streams detailed logs to `/videos/logs/<jobId>.log`, which the API tails for the status endpoint.

## Smoke Test

Replace placeholders with your host/IP, project ID, and asset paths.

```bash
PID=p_example
BASE=http://192.168.86.23:8082

curl -sS -X PUT "$BASE/v1/projects/$PID/scenes" \
  -H "Content-Type: application/json" \
  --data-binary @data/scenes.json

curl -sS -X POST "$BASE/v1/projects/$PID/assets" \
  -F "files=@assets/images/sample.jpg" \
  -F "subdir=images"

JOB=$(curl -sS -X POST "$BASE/v1/projects/$PID/render" \
  -H "Content-Type: application/json" \
  -d '{"outputName":"demo.mp4","renderOptions":{"fps":30,"minShot":2.5,"maxShot":8.0,"xfade":0.5,"crf":18,"preset":"medium","tts":null,"voiceDir":"voiceovers","music":null,"ducking":false}}' | jq -r '.jobId')

echo "Queued job: $JOB"

curl -sS "$BASE/v1/jobs/$JOB"

curl -sS "$BASE/v1/projects/$PID/outputs/video" -o dist/demo.mp4
```

## Development Notes

- The Docker image installs `ffmpeg`, `jq`, and `tini`; additional build deps can be added in `render-api/Dockerfile` if needed.
- `render-api/app/renderer.py` is the FFmpeg orchestration point; extend it for music beds, ducking, or additional effects.
- Logs and intermediates remain in `/videos/projects/<projectId>/work` on failure for debugging. Successful runs leave outputs in `/videos/projects/<projectId>/output` and trace logs in `/videos/logs`.

## License

This repository currently has no explicit license. Add one if you plan to distribute or share the project.
 download and check:
 scp -r master-benjamin@192.168.86.23:~/Videos/projects/ \
    ~/Downloads/tmp

    scp -r master-benjamin@192.168.86.23:/home/master-benjamin/Projects/rs-video-stitch/data/projects/ \
    ~/Downloads/tmp
