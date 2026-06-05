# GGUF llama.cpp OpenAI-compatible server, offline.
#
# This is a *skeleton* for an air-gapped build. In a connected build environment
# the binary and weights are fetched once; on the air-gapped host nothing here
# reaches the network. Model weights are NOT baked in -- they are mounted
# read-only from the pre-staged `model-weights` volume at runtime (see
# docker/docker-compose.offline.yml).
#
# Build arg lets you pin a llama.cpp server image you have already vendored into
# a local registry / mirror, so the build itself can run offline.

ARG LLAMACPP_IMAGE=ghcr.io/ggml-org/llama.cpp:server
FROM ${LLAMACPP_IMAGE}

# --- Runtime configuration (overridable via the compose `environment:` block) ---
# Path to a pre-staged GGUF inside the read-only weights volume.
ENV LLAMA_MODEL_PATH=/models/model.gguf \
    LLAMA_HOST=0.0.0.0 \
    LLAMA_PORT=8000 \
    LLAMA_CTX_SIZE=8192 \
    # Belt-and-suspenders: tell any tooling in the image not to phone home.
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# The server binds 0.0.0.0 *inside* the container; the host side is pinned to
# 127.0.0.1 by the compose port mapping, so this is loopback-only on the host.
EXPOSE 8000

# llama.cpp's server exposes an OpenAI-compatible API at /v1 and a /health probe.
# `--no-webui` keeps the surface minimal; weights come from the mounted volume.
ENTRYPOINT ["/bin/sh", "-c", \
  "exec llama-server \
     --model \"${LLAMA_MODEL_PATH}\" \
     --host \"${LLAMA_HOST}\" \
     --port \"${LLAMA_PORT}\" \
     --ctx-size \"${LLAMA_CTX_SIZE}\" \
     --no-webui"]
