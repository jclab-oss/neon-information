#!/usr/bin/env bash
# pack-output.sh <image>
#
# Archives the tests' output of NEON_TEST_DIR (see run.sh) to `test_output.tar.gz` there, readable by the runner's
# user (e.g. to upload it). <image> is any image with tar, e.g. the one the tests ran in.
set -euo pipefail

image=$1
: "${NEON_TEST_DIR:?}"

if [ -d "${NEON_TEST_DIR}/test_output" ]; then
    # pg_dynshmem links to /dev/shm
    docker run --rm --user root --volume "${NEON_TEST_DIR}:/neon-test" "${image}" \
        tar -C /neon-test -czf /neon-test/test_output.tar.gz --exclude=pg_dynshmem test_output || true
fi
