# eSCL scanner simulator (dev / CI). Build context: tools/escl-sim
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ESCL_SIM_SAMPLES_DIR=/samples

WORKDIR /app
COPY pyproject.toml ./
COPY escl_sim ./escl_sim
RUN pip install . && useradd --system --uid 10001 sim && mkdir -p /samples
COPY samples /samples

USER sim
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/_sim/state', timeout=2)"

ENTRYPOINT ["python", "-m", "escl_sim", "--host", "0.0.0.0", "--port", "8090"]
# extra flags, e.g.: docker run ... escl-sim --stack invoice_qr --offline-mode 503
CMD []
