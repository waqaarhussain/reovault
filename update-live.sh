#!/usr/bin/env bash
# reovault-update-live
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then exec sudo bash "$0" "$@"; fi
if [[ $# -gt 0 ]]; then
  echo 'Usage: update-live (or reovault update)' >&2
  exit 1
fi
umask 077
stage_dir="$(mktemp -d /tmp/reovault-update.XXXXXX)"
trap 'rm -rf -- "$stage_dir"' EXIT
curl -fsSL --retry 3 --connect-timeout 15 --max-time 90 \
  https://raw.githubusercontent.com/waqaarhussain/reovault/main/bootstrap.sh \
  -o "$stage_dir/bootstrap.sh"
bash "$stage_dir/bootstrap.sh" --update
