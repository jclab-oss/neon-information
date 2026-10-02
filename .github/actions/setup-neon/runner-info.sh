#!/usr/bin/env bash
# Prints a Markdown line describing the runner, for the reports: its image, CPUs, memory and free disk
set -euo pipefail

cpu=$(lscpu | sed -n 's/^Model name: *//p' | head -n 1)
memory=$(awk '/^MemTotal:/ { printf "%.1f", $2 / 1048576 }' /proc/meminfo)
disk=$(df -BG --output=avail / | tail -n 1 | tr -dc '0-9')
image="${ImageOS:-unknown image}${ImageVersion:+ ${ImageVersion}}"
echo "Runner: ${RUNNER_ENVIRONMENT:-unknown} \`${image}\`, ${RUNNER_ARCH:-$(uname -m)}, $(nproc) vCPU (${cpu:-unknown CPU}), ${memory} GiB memory, ${disk} GiB free disk"
