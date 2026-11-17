FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd
COPY memory_core/requirements.txt /work/memory_core/requirements.txt
COPY memory_web/requirements*.txt /work/memory_web/
RUN pip install --no-cache-dir -r /work/memory_web/requirements-test.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /work
COPY memory_core /work/memory_core
COPY memory_web /work/memory_web
