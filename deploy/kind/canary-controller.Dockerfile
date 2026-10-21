# Local kind image for the canary controller. Do not push this image and do not run it on GKE.
# Build from the repository root:
#   docker build -f deploy/kind/canary-controller.Dockerfile -t platformforge/canary-controller:0.1.0 .

ARG BUILD_IMAGE=python:3.13-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG RUNTIME_IMAGE=gcr.io/distroless/python3-debian13:nonroot@sha256:ed91505d162119a1e46552bc7f471c77737729a525269a7e9b60646807564601

FROM ${BUILD_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --only-binary=:all: --target /deps /src

FROM ${RUNTIME_IMAGE}
ENV PYTHONPATH=/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=build /deps /deps
COPY deploy/istio/platformforge-dev/inventory-virtualservice-stable.yaml /routes/inventory-virtualservice-stable.yaml
COPY deploy/istio/platformforge-dev/inventory-virtualservice-canary.yaml /routes/inventory-virtualservice-canary.yaml
COPY deploy/istio/platformforge-dev/inventory-virtualservice-promote.yaml /routes/inventory-virtualservice-promote.yaml
ENV PLATFORMFORGE_MESH_DIR=/routes
USER 65532:65532
EXPOSE 8080
CMD ["-m", "platformforge.canary_controller"]
