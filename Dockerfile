FROM python:3.12-slim

# Unbuffered stdout so `docker compose logs` shows worker output as it happens
# rather than holding it in a pipe buffer until the process exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Every service overrides this with its own `command:` in docker-compose.yml.
CMD ["python", "orchestrator.py"]
