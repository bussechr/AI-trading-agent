FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

WORKDIR /workspace

COPY pyproject.toml /workspace/pyproject.toml
COPY src /workspace/src
COPY scripts /workspace/scripts

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e . mlflow

ENV FXSTACK_MLFLOW_ENABLED=1
ENV FXSTACK_SEQUENCE_DATASET_CACHE_ROOT=/workspace/artifacts/sequence_cache

CMD ["python", "-m", "fxstack.research.sequence_runner"]
