# The Python backend: data layer, engine, verdict, worker.
#
# Runs on the Oracle box (ARM64) and on a laptop unchanged, because the worker
# is defined entirely by "polls the jobs table" -- there is no separate local
# mode to maintain.
#
# Strategies are Pine, run through PineForge: its transpiler (pineforge-codegen,
# PineForge Source License) turns a script into C++, g++ compiles that against
# its engine (libpineforge.a, Apache-2.0), and btb/pine/native/btb_shim.cpp is
# the one C++ file of ours, loading the result for Python. Engine and
# transpiler are pinned to the same release: PineForge supports exactly one
# engine/codegen pair per version.
ARG PINEFORGE_VERSION=1.6.0

FROM ghcr.io/pineforge-4pass/pineforge-release:${PINEFORGE_VERSION} AS pineforge

FROM python:3.11-slim-bookworm
ARG PINEFORGE_VERSION

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    PINEFORGE_PREFIX=/opt/pineforge \
    PINEFORGE_ENGINE_VERSION=${PINEFORGE_VERSION} \
    BTB_PINE_SHIM=/opt/btb/libbtb_shim.so \
    BTB_PINE_CACHE=/var/cache/btb-pine

RUN apt-get update && apt-get install -y --no-install-recommends g++ libeigen3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pineforge /opt/pineforge/lib /opt/pineforge/lib
COPY --from=pineforge /opt/pineforge/include /opt/pineforge/include
RUN echo "${PINEFORGE_VERSION}" > /opt/pineforge/VERSION

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "pineforge-codegen==${PINEFORGE_VERSION}"

COPY btb/pine/native/btb_shim.cpp /opt/btb/btb_shim.cpp
RUN g++ -std=c++17 -O2 -fPIC -shared -I/opt/pineforge/include /opt/btb/btb_shim.cpp \
        -ldl -o /opt/btb/libbtb_shim.so \
    && mkdir -p /var/cache/btb-pine

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
