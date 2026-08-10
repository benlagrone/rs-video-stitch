# Pipeline And Fortress LAN

This repository is the active git home for the MediaStudio render surface:

- `render-api/`: FastAPI + worker + FFmpeg renderer.
- `render-ui/`: React/Vite browser interface for uploading images, pasting scripts, queueing renders, and downloading MP4s.

The render API also exposes `POST /v1/script/enhance` for script rewriting through an
Ollama-compatible API. Configure it with `OLLAMA_BASE_URL` and `OLLAMA_MODEL`
(`mixtral:latest` on Fortress LAN).

## CI

GitHub Actions workflow:

```text
.github/workflows/ci.yml
```

It runs on pushes and pull requests to `main`:

- installs and tests `render-api`
- installs and builds `render-ui`
- builds the render API Docker image

## Local UI Development

```bash
cd render-ui
npm install
npm run dev
```

The UI defaults to:

```text
http://localhost:8082
```

for the Render API.

## Fortress LAN Deployment Boundary

Fortress LAN should consume this repo as the source artifact, but runtime wiring belongs in:

```text
/Users/benjaminlagrone/Documents/projects/fortress-lan
```

Do not add Fortress runtime compose, routing, public ports, rollback, or host service ownership to this repo. Add a targeted `mediastudio` deploy service in `fortress-lan` when ready, then have that deploy service build or pull this repo's render API image and serve the built `render-ui` assets under `fortress.lan`.
