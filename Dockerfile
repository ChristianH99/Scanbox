# syntax=docker/dockerfile:1.7
# Scanbox – one image: FastAPI backend + built Svelte frontend + OCR toolchain.
# Targets: runtime (default), test (backend test suite incl. OCR tests).

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

# ---------------------------------------------------------------- frontend build
FROM node:${NODE_VERSION}-bookworm-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
COPY docs/openapi.json /src/docs/openapi.json
RUN npm run build

# ---------------------------------------------------------------- python deps
FROM python:${PYTHON_VERSION}-slim-bookworm AS pydeps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src/backend
COPY backend/pyproject.toml ./
# Install dependencies first (cached layer), then the package itself.
RUN mkdir scanbox && echo '__version__ = "0.0.0"' > scanbox/__init__.py \
 && pip install . && pip uninstall -y scanbox
COPY backend/ ./
RUN pip install --no-deps .

# ---------------------------------------------------------------- base (OS deps + venv)
FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Scanbox" \
      org.opencontainers.image.description="Self-hosted LAN scanning app for eSCL scanners (HP ScanJet Pro N4000)" \
      org.opencontainers.image.source="https://github.com/ChristianH99/Scanbox" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-osd \
      ghostscript qpdf unpaper pngquant libglib2.0-0 tini \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 scanbox && useradd --uid 1000 --gid 1000 --home-dir /data --no-create-home scanbox \
 && mkdir -p /data && chown scanbox:scanbox /data

COPY --from=pydeps /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCANBOX_DATA_DIR=/data \
    SCANBOX_STATIC_DIR=/app/static \
    SCANBOX_PORT=8080 \
    OMP_THREAD_LIMIT=1 \
    SCANBOX_VERSION=${VERSION}

# ---------------------------------------------------------------- tests (CI)
FROM base AS test
COPY backend/ /src/backend/
COPY tools/escl-sim/ /src/tools/escl-sim/
COPY docs/ /src/docs/
RUN pip install --no-cache-dir "/src/backend[dev]" "/src/tools/escl-sim[test]" \
 && chown -R scanbox:scanbox /src
USER scanbox
WORKDIR /src/backend
ENV HOME=/tmp
ENTRYPOINT []
CMD ["sh", "-c", "python -m pytest -q -p no:cacheprovider tests && cd ../tools/escl-sim && python -m pytest -q -p no:cacheprovider tests"]

# ---------------------------------------------------------------- runtime (default)
FROM base AS runtime
COPY --from=frontend /src/frontend/dist /app/static
USER scanbox
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "scanbox"]
