FROM node:22-slim AS render-ui-build

WORKDIR /ui
COPY render-ui/package*.json ./
RUN npm ci
COPY render-ui ./
RUN npm run build

FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    blender \
    ffmpeg \
    jq \
    tini \
    libasound2 \
    libcurl4 \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY render-api/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY render-api/app /srv/app
COPY media /srv/media
COPY --from=render-ui-build /usr/local/bin/node /usr/local/bin/node
COPY --from=render-ui-build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY hyperframes/package.json /srv/hyperframes/package.json
RUN npm install --prefix /srv/hyperframes --omit=dev
COPY hyperframes /srv/hyperframes
COPY --from=render-ui-build /ui/dist /srv/render-ui
ENV PYTHONUNBUFFERED=1
ENV RENDER_STORAGE=/videos
ENV DB_URL=sqlite:////videos/db.sqlite3
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV HYPERFRAMES_PROJECT_DIR=/srv/hyperframes

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]
