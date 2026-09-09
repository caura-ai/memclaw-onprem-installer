# On-prem core-api image variant with sentence-transformers + BGE pre-loaded.
#
# Why a separate image:
#   - The base ghcr.io/caura-ai/memclaw-core-api image is slim (≈750MB)
#     because SaaS customers almost always set OPENAI_API_KEY or
#     PLATFORM_EMBEDDING_API_KEY and never hit the LocalEmbedding path.
#   - On-prem + air-gapped customers have no API key and no internet, so
#     EMBEDDING_PROVIDER=local is the only option. LocalEmbedding
#     lazy-imports sentence-transformers which isn't in the base image —
#     that's why the recall request returned 503 in the first E2E run.
#   - This image adds sentence-transformers (~1.1GB after torch) + the
#     default BAAI/bge-base-en-v1.5 model weights (~400MB), pre-downloaded
#     at build time so the container starts offline.
#
# Skip this image entirely when:
#   - You've set OPENAI_API_KEY (or PLATFORM_EMBEDDING_API_KEY) and don't
#     need local embeddings. The base core-api image is plenty.
#
# Use this image when:
#   - Air-gapped deployment OR you want Caura to run without any
#     external LLM dependency.
#
# Wire up via docker-compose.embedder.yml overlay — see the bundle README.

ARG BASE_IMAGE=ghcr.io/caura-ai/memclaw-core-api:latest
FROM ${BASE_IMAGE}

USER root

# sentence-transformers pulls torch (CPU-only wheel) + transformers +
# huggingface-hub. Pin torch to the CPU wheel to save ~1.5GB vs the GPU
# default. Installs go into the base image's site-packages so imports
# resolve without touching any other layer.
RUN pip install --no-cache-dir \
      "sentence-transformers>=3.0,<4.0" \
      "torch>=2.0,<3.0" \
    && python -c "import sentence_transformers; print('sentence-transformers:', sentence_transformers.__version__)"

# Pre-download the default model so the container boots offline.
# Sets HF_HOME to a path the appuser owns so runtime loads don't re-fetch.
ENV HF_HOME=/app/.cache/huggingface \
    TRANSFORMERS_OFFLINE=0
RUN mkdir -p "$HF_HOME" && \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-en-v1.5')" && \
    chown -R appuser:appuser /app/.cache

# Flip the offline flag so runtime NEVER tries to hit huggingface.co —
# critical for air-gap customers. If they explicitly want a different
# model they override EMBEDDING_PROVIDER away from local.
ENV TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1

USER appuser
