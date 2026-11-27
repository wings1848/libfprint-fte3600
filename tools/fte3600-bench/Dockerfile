# SPDX-License-Identifier: LGPL-2.1-or-later
# Self-contained offline multi-algorithm matcher benchmark for FTE3600 (64x80).
# Build from repository root:
#   docker build -f tools/fte3600-bench/Dockerfile -t fte3600-bench .

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    FTE3600_BRISK_CLI=/usr/local/bin/brisk_cli

RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    gcc \
    libc6-dev \
    pkg-config \
    libglib2.0-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy 'opencv-python-headless<5'

WORKDIR /opt/fte3600-bench

COPY libfprint/drivers/fte3600/sam7-brisk/libfprint/ /opt/fte3600-bench/sam7-brisk/libfprint/
COPY tools/fte3600-bench/brisk_cli.c /opt/fte3600-bench/brisk_cli.c
COPY tools/fte3600-bench/bench.py /opt/fte3600-bench/bench.py

RUN gcc -O2 -Wall -Wextra -Werror \
    -DFTE3600_ENABLE_PERSONAL_AUTH=1 \
    -fno-fast-math -ffp-contract=off -fexcess-precision=standard -frounding-math \
    -I/opt/fte3600-bench/sam7-brisk/libfprint/drivers \
    -I/opt/fte3600-bench/sam7-brisk/libfprint/matchers/brisk \
    $(pkg-config --cflags glib-2.0) \
    /opt/fte3600-bench/brisk_cli.c \
    /opt/fte3600-bench/sam7-brisk/libfprint/matchers/brisk/brisk.c \
    /opt/fte3600-bench/sam7-brisk/libfprint/drivers/fte3600-brisk.c \
    -o /usr/local/bin/brisk_cli \
    $(pkg-config --libs glib-2.0) -lm \
    && chmod 0755 /opt/fte3600-bench/bench.py

ENTRYPOINT ["python3", "/opt/fte3600-bench/bench.py"]
CMD ["--synthetic"]
