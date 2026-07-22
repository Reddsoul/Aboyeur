FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Code lives under the `backend` package so `import backend.x` resolves (PYTHONPATH=/app).
COPY . /app/backend
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
