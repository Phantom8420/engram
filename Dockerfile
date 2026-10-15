FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engram engram
COPY web web
EXPOSE 8000
CMD ["uvicorn", "engram.server:app", "--host", "0.0.0.0", "--port", "8000"]
