#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
# Build and run the FTE3600 offline multi-algorithm matcher benchmark in Docker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
IMAGE_NAME="${FTE3600_BENCH_IMAGE:-fte3600-bench:latest}"
DO_BUILD=1

usage() {
  cat <<'EOF'
Usage:
  run-docker.sh [--no-build] [--synthetic] [bench.py args...]
  run-docker.sh [--no-build] <dump_dir> [bench.py args...]
  run-docker.sh [--no-build] --dataset <dump_dir> [bench.py args...]

Examples:
  ./tools/fte3600-bench/run-docker.sh
  ./tools/fte3600-bench/run-docker.sh --synthetic --fingers 10 --captures 16
  ./tools/fte3600-bench/run-docker.sh /path/to/FTE3600_DUMP_DIR
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--no-build" ]]; then
  DO_BUILD=0
  shift
fi

DATASET_DIR=""
EXTRA_ARGS=()

if [[ $# -eq 0 ]]; then
  EXTRA_ARGS=(--synthetic)
elif [[ "${1}" == "--dataset" ]]; then
  if [[ $# -lt 2 ]]; then
    echo "error: --dataset requires a directory path" >&2
    exit 2
  fi
  DATASET_DIR="$2"
  shift 2
  EXTRA_ARGS=(--dataset /data "$@")
elif [[ "${1}" != -* && -d "${1}" ]]; then
  DATASET_DIR="$1"
  shift
  EXTRA_ARGS=(--dataset /data "$@")
else
  EXTRA_ARGS=("$@")
fi

if [[ "${DO_BUILD}" -eq 1 ]]; then
  docker build -f "${SCRIPT_DIR}/Dockerfile" -t "${IMAGE_NAME}" "${REPO_ROOT}"
fi

DOCKER_ARGS=(--rm)
if [[ -n "${DATASET_DIR}" ]]; then
  ABS_DATA="$(cd "${DATASET_DIR}" && pwd)"
  DOCKER_ARGS+=(-v "${ABS_DATA}:/data:ro")
fi

exec docker run "${DOCKER_ARGS[@]}" "${IMAGE_NAME}" "${EXTRA_ARGS[@]}"
