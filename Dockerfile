# The Internet Daily container: TeX Live + Python + rmapi + the project
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System packages: a minimal TeX Live (xelatex + fontspec + microtype + multicol
# + amsmath + needspace + Latin Modern) and poppler for previews. fonts-cmu is
# Computer Modern Unicode: the same design as Latin Modern but with Cyrillic,
# without which a Russian-language headline renders as blank space. We no longer
# ask for Python here: bookworm ships 3.11 and the project needs 3.14, so uv
# downloads and manages the interpreter itself (see below). texlive-latex-extra
# still drags in a system python3 for its own scripts — the app never uses it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        texlive-xetex texlive-fonts-recommended texlive-latex-extra \
        texlive-lang-european \
        lmodern \
        fonts-cmu \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# uv: resolver + installer + Python provisioner, pinned to a release tag.
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv

# rmapi: reMarkable API client — static Go binary, no runtime deps.
# Pinned + checksummed per architecture; bump all three when upgrading.
# Release assets at https://github.com/ddvk/rmapi/releases. TARGETARCH is
# set automatically by BuildKit (amd64 on x86_64 hosts, arm64 on Apple
# Silicon / Ampere / Raspberry Pi etc.) so `docker build` on either host
# fetches the correct binary.
ARG TARGETARCH
ARG RMAPI_VERSION=0.0.34
ARG RMAPI_SHA256_AMD64=3e17c4a4d529a9e71eaa970b64d9cfbf2dd2cb16c55c4d397d6d821e135c9fae
ARG RMAPI_SHA256_ARM64=c204fa7650ba9091fd1c0b05cb32f1d564247d3f964ae8c43e1084fa19639375
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) sha="${RMAPI_SHA256_AMD64}" ;; \
        arm64) sha="${RMAPI_SHA256_ARM64}" ;; \
        *) echo "rmapi: unsupported TARGETARCH '${TARGETARCH}'" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/rmapi.tgz \
        "https://github.com/ddvk/rmapi/releases/download/v${RMAPI_VERSION}/rmapi-linux-${TARGETARCH}.tar.gz"; \
    echo "${sha}  /tmp/rmapi.tgz" | sha256sum -c -; \
    tar -xz -C /usr/local/bin -f /tmp/rmapi.tgz rmapi; \
    chmod +x /usr/local/bin/rmapi; \
    rm /tmp/rmapi.tgz

WORKDIR /app

# Interpreter + venv live outside /app so a bind-mounted source tree during
# development can't shadow them.
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN uv python install 3.14

# Dependencies first, from the lockfile, so the layer caches across source
# edits. --frozen fails the build if uv.lock has drifted from pyproject.toml.
COPY pyproject.toml uv.lock .python-version ./
# --all-extras so the optional store/queue backends (pymongo, arq) are
# present when a compose overlay switches to them. Both are small and the
# defaults still touch neither.
RUN uv sync --frozen --no-install-project --no-dev --all-extras

COPY tid ./tid
COPY sources.toml ./
RUN uv sync --frozen --no-dev --all-extras

# State + cache live on a mounted volume.
RUN mkdir -p /data/archive/cache
ENV TID_STATE=/data/state.db
ENV TID_CONFIG=/app/sources.toml
ENV TID_CACHE=/data/archive/cache

EXPOSE 8000

# One uvicorn worker: APScheduler runs in-process, so multiple workers would
# multiply ingest runs. Concurrency comes from the event loop now, not threads.
CMD ["/opt/venv/bin/uvicorn", \
     "tid.web:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--timeout-graceful-shutdown", "30"]
