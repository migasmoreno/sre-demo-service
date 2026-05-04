FROM python:3.12-slim

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT=8080
EXPOSE 8080

# Gunicorn: gthread workers (threads, not forks) to ensure gRPC-based
# CloudTraceSpanExporter is not broken by fork(). Cloud Run scales at the
# container level so 1 worker + 4 threads is the right model here.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--worker-class", "gthread", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "main:app"]
