FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY gold_agent ./gold_agent
RUN pip install --no-cache-dir .
EXPOSE 8020
CMD ["uvicorn", "gold_agent.api:app", "--host", "0.0.0.0", "--port", "8020"]
