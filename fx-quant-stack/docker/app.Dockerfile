FROM python:3.11-slim
WORKDIR /workspace
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .
COPY . .
CMD ["uvicorn", "fxstack.api.app:app", "--host", "0.0.0.0", "--port", "58710"]
