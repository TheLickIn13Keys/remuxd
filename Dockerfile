# remuxd: on-demand remux/transcode of video URLs to seekable HLS.
#
#   docker build -t remuxd .
#   docker run --rm -p 8000:8000 remuxd
#   docker run --rm -p 8000:8000 --env-file .env -e REMUXD_DEMO=1 remuxd
#
# remuxd has NO authentication and fetches whatever URL it is handed, so
# publishing this port exposes a service that will make HTTP requests from
# inside your network. Bind it to localhost (-p 127.0.0.1:8000:8000) or put an
# authenticating reverse proxy in front of it.

# --- build the wheel ------------------------------------------------------
# Separate stage so pip/setuptools never land in the runtime image.
FROM python:3.12-slim-bookworm AS build

WORKDIR /src
RUN pip install --no-cache-dir build==1.2.2.post1

# pyproject alone is enough metadata to build; src/ carries the package and its
# bundled demo assets (declared via package-data).
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist


# --- runtime --------------------------------------------------------------
FROM python:3.12-slim-bookworm

# ffmpeg + ffprobe are the only runtime requirement (remuxd itself is stdlib
# only). The fonts are for the ASS style-font pass: it serves locally installed
# fonts that a release names but doesn't embed, and a slim image has none at
# all. These two are a floor, not a fix -- matching is by FILENAME, so a style
# naming "Arial" won't resolve against LiberationSans-Regular.ttf. For real
# coverage mount fonts into a subdirectory of /usr/share/fonts (scanned
# recursively; a subdirectory so the image's own fonts stay visible):
#
#   -v /usr/share/fonts:/usr/share/fonts/host:ro   # Linux host's fonts
#   -v ~/Library/Fonts:/usr/share/fonts/host:ro    # macOS user fonts
#   -v "$PWD/fonts":/usr/share/fonts/host:ro       # your own collection
#
# macOS note: /System/Library/Fonts sits on the read-only system volume and
# Docker Desktop cannot share it; copy what you need into a folder instead.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-liberation \
        fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Plain COPY rather than a --mount=from cache mount, so this builds on legacy
# (non-BuildKit) builders too. The wheel is ~2MB, almost all bundled demo assets.
COPY --from=build /dist /tmp/dist
RUN pip install --no-cache-dir /tmp/dist/*.whl && rm -rf /tmp/dist

# Unprivileged, with a writable session root. Sessions are pure scratch (HLS
# fragments, extracted subs/fonts) and are cleaned up on idle and on shutdown,
# so mount a tmpfs here if you'd rather they never touch disk:
#   --tmpfs /var/lib/remuxd/sessions:size=4g,mode=1777
# mode=1777 matters: a tmpfs mounts root-owned 755 over this path and hides the
# ownership set below, leaving the unprivileged user unable to create sessions.
RUN useradd --system --uid 10001 --create-home --home-dir /home/remuxd remuxd \
 && mkdir -p /var/lib/remuxd/sessions \
 && chown -R remuxd:remuxd /var/lib/remuxd

# REMUXD_HOST: the 127.0.0.1 default would only be reachable from inside the
# container, so published ports would connect to nothing.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REMUXD_HOST=0.0.0.0 \
    REMUXD_PORT=8000 \
    REMUXD_SESSION_ROOT=/var/lib/remuxd/sessions

USER remuxd
WORKDIR /home/remuxd
EXPOSE 8000

# "/" answers a JSON descriptor with the demo off, so it needs no session.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/' % os.environ.get('REMUXD_PORT','8000'), timeout=4)" || exit 1

# Exec form, so remuxd is PID 1 and receives SIGTERM directly. It installs its
# own SIGTERM handler and reaps sessions on the way out, so `docker stop` is a
# clean shutdown and needs no init shim.
ENTRYPOINT ["remuxd"]
