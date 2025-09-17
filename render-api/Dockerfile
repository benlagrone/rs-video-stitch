FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg jq tini && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /srv/app
ENV PYTHONUNBUFFERED=1
ENV RENDER_STORAGE=/videos
ENV DB_URL=sqlite:////videos/db.sqlite3

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]
