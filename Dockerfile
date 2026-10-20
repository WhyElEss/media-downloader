# Builds natively for the target platform (linux/arm64 on a Raspberry Pi).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/app/bin:$PATH

# ffmpeg converts and muxes; gosu drops root after the entrypoint fixes ownership.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gosu tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Two venvs: the web app's dependencies stay fixed, the engines venv is
# upgraded in place (on start and from the UI) without touching the app.
COPY requirements.txt engines.txt /tmp/
RUN python -m venv /opt/app && /opt/app/bin/pip install -r /tmp/requirements.txt \
 && python -m venv /opt/engines && /opt/engines/bin/pip install --upgrade pip \
 && /opt/engines/bin/pip install --upgrade -r /tmp/engines.txt \
 # Writable for whatever PUID runs the container, so start-up does not need
 # a slow chown -R before engines can be upgraded in place.
 && chmod -R a+rwX /opt/engines

WORKDIR /srv
COPY app ./app
COPY web ./web
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8790
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["serve"]
