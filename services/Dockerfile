# syntax=docker/dockerfile:1
#
# Build one image per service with --target:
#   docker build --target inventory-service -t platformforge/inventory-service:0.1.0 services
#   docker build --target orders-service    -t platformforge/orders-service:0.1.0    services
#
# Both base images are Python 3.13 so compiled wheels (pydantic-core) match the runtime.

ARG BUILD_IMAGE=python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG RUNTIME_IMAGE=gcr.io/distroless/python3-debian13:nonroot@sha256:ed91505d162119a1e46552bc7f471c77737729a525269a7e9b60646807564601

FROM ${BUILD_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN pip install --only-binary=:all: --target /deps -r /tmp/requirements.txt
COPY pf_service_common /app/pf_service_common
COPY inventory_service /app/inventory_service
COPY orders_service /app/orders_service
RUN python -m compileall -q /app

# Distroless: no shell or package manager; runs as UID/GID 65532 (nonroot).
FROM ${RUNTIME_IMAGE} AS runtime
ENV PYTHONPATH=/app:/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /deps /deps
COPY --from=build /app/pf_service_common /app/pf_service_common
USER 65532:65532
EXPOSE 8080

FROM runtime AS inventory-service
ARG VERSION=0.1.0
ARG GIT_SHA=unknown
ENV SERVICE_VERSION=${VERSION} GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.title="inventory-service" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
COPY --from=build /app/inventory_service /app/inventory_service
CMD ["-m", "inventory_service"]

FROM runtime AS orders-service
ARG VERSION=0.1.0
ARG GIT_SHA=unknown
ENV SERVICE_VERSION=${VERSION} GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.title="orders-service" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
COPY --from=build /app/orders_service /app/orders_service
CMD ["-m", "orders_service"]
