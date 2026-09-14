# The Python backend: data layer now, engine and worker later.
#
# Runs on the Oracle box (ARM64) and on a laptop unchanged, because the worker
# is defined entirely by "polls the jobs table" -- there is no separate local
# mode to maintain.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY btb/ ./btb/
COPY universe.json .

# Defaults for running inside the Docker network. The DSN is assembled in
# btb/db.py; the password comes from the environment, never from the image.
ENV BTB_DB_HOST=btb-postgres \
    BTB_DB_PORT=5432 \
    BTB_DB_NAME=btb \
    BTB_DB_USER=btb

ENTRYPOINT ["python", "-m"]
CMD ["btb.data", "list"]
