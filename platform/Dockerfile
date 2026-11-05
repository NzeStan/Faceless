# Build from the repository root: docker build -f platform/Dockerfile --target api .
FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/platform
RUN useradd --create-home --uid 1000 app
COPY packages/django-social-publisher /srv/packages/django-social-publisher
COPY platform/pyproject.toml /srv/platform/pyproject.toml
RUN pip install --retries 10 --timeout 60 "/srv/packages/django-social-publisher[celery,drf]" \
    && python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb'))['project']; open('/tmp/r.txt','w').write('\n'.join(x for x in d['dependencies'] if not x.startswith('django-social-publisher')))" \
    && pip install --retries 10 --timeout 60 -r /tmp/r.txt
COPY platform /srv/platform

FROM base AS api
USER app
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]

FROM base AS render
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
USER app
CMD ["celery", "-A", "config", "worker", "-Q", "render", "--concurrency", "1", "--loglevel", "INFO"]

FROM render AS test
USER root
RUN pip install --retries 10 --timeout 60 pytest pytest-django factory-boy respx ruff
USER app
CMD ["pytest"]
