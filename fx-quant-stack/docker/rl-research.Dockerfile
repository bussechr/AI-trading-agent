FROM python:3.11-slim

WORKDIR /workspace

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e ".[rl_research,research]" mlflow

COPY src /workspace/src
COPY scripts /workspace/scripts

ENV FXSTACK_MLFLOW_ENABLED=1
ENV FXSTACK_RL_ARTIFACT_ROOT=/workspace/artifacts/rl
ENV FXSTACK_RL_DATASET_ROOT=/workspace/artifacts/rl/datasets

CMD ["python", "-m", "fxstack.rl.evaluate", "--help"]
