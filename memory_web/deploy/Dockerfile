FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /work
# Optional mirror for servers that cannot reach PyPI quickly; empty keeps pip's default index.
ARG PIP_INDEX_URL=
COPY memory_core/requirements.txt /work/memory_core/requirements.txt
COPY memory_web/requirements.txt /work/memory_web/requirements.txt
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r memory_web/requirements.txt \
    && useradd --uid 10001 --create-home memory \
    && mkdir -p /var/lib/memory-admin \
    && chown memory:memory /var/lib/memory-admin \
    && chmod 700 /var/lib/memory-admin
COPY memory_core /work/memory_core
COPY memory_web /work/memory_web
USER 10001:10001
CMD ["python", "-m", "memory_web.deploy.runtime", "serve"]
