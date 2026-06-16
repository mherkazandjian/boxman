# Image for the boxman HTTP API server and Celery worker.
#
# The boxman source itself is bind-mounted at /boxman (see docker-compose.yml)
# and made importable via PYTHONPATH, mirroring the dev workflow. Only the
# third-party dependencies are baked into the image. The worker also needs the
# docker CLI so the docker-compose runtime can `docker exec` into the sibling
# boxman-libvirt container.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/boxman/src

# docker CLI (client only) for the docker-compose runtime; plus libvirt/ssh
# client tools used by some boxman read paths.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        docker.io openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Third-party deps (boxman core + the `api` extra). Pinned loosely to match
# pyproject; the boxman package itself comes from the bind mount.
RUN pip install --no-cache-dir \
        PyYAML invoke Jinja2 lxml passlib bcrypt \
        fastapi "uvicorn[standard]" pydantic-settings \
        "celery[redis]" redis SQLAlchemy alembic \
        "python-jose[cryptography]" python-multipart

WORKDIR /boxman
EXPOSE 8080

# Default to the API server; the worker service overrides `command`.
CMD ["uvicorn", "boxman.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
