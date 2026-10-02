#!/usr/bin/env bash
# run.sh <image> <script> [docker run options...]
#
# Runs the bash <script> in <image> (Neon's build-tools image, or one based on it), in the tests checked out by the
# setup-neon action (NEON_TEST_SRC), with the build's binaries (NEON_BIN, POSTGRES_DISTRIB_DIR) of NEON_TEST_DIR,
# `/neon-test` in the container, the tests' output in its `test_output` (TEST_OUTPUT) and the Postgres version
# PG_VERSION (`v17`). The pytest plugins of this directory are importable (`-p without_testing_feature`).
# Exits with the script's exit code.
set -euo pipefail

image=$1
script=$2
shift 2
: "${NEON_TEST_DIR:?}" "${NEON_TEST_SRC:?}" "${PG_VERSION:?}"

mkdir -p "${NEON_TEST_DIR}/test_output"
# The build-tools image runs as `nonroot` (uid 1000)
chmod -R a+rwX "${NEON_TEST_SRC}" "${NEON_TEST_DIR}"

docker pull --quiet "${image}"
# The Postgres installations are built in /home/nonroot/pg_install (build-tools' user's home), which is their
# RUNPATH (e.g. neon.so's, for libpq): the build's are mounted there (a previous build's use their libraries).
# Host networking: compute_ctl listens on `[::]`, which needs IPv6 (see setup-neon).
exec docker run --rm --network host \
    --volume "${NEON_TEST_SRC}:/src" --workdir /src \
    --volume "${NEON_TEST_DIR}:/neon-test" \
    --volume "${NEON_TEST_DIR}/neon/pg_install:/home/nonroot/pg_install:ro" \
    --volume "$(cd "$(dirname "$0")" && pwd):/neon-test-plugins:ro" \
    --env CI=true \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --env PYTHONPATH=/neon-test-plugins \
    --env NEON_BIN=/neon-test/neon/bin \
    --env POSTGRES_DISTRIB_DIR=/home/nonroot/pg_install \
    --env TEST_OUTPUT=/neon-test/test_output \
    --env BUILD_TYPE=release \
    --env DEFAULT_PG_VERSION="${PG_VERSION#v}" \
    "$@" \
    "${image}" \
    bash -euxo pipefail -c "${script}"
