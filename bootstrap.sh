#!/usr/bin/env bash
# ReoVault GitHub bootstrap. Download this file, then run it with sudo bash.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then exec sudo bash "$0" "$@"; fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 22.04 ]]; then
  echo 'ReoVault targets Ubuntu 22.04.' >&2
  exit 1
fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --update ) ]]; then
  echo 'Usage: bootstrap.sh [--update]' >&2
  exit 1
fi
if [[ "${1:-}" == --update && ! -f /etc/reovault/config.json ]]; then
  echo 'ReoVault is not installed. Run the bootstrap command first.' >&2
  exit 1
fi
for dependency in curl python3 tar; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y curl ca-certificates python3 tar
    break
  fi
done
umask 077
stage_dir="$(mktemp -d /tmp/reovault-bootstrap.XXXXXX)"
trap 'rm -rf -- "$stage_dir"' EXIT
repo_url='https://api.github.com/repos/waqaarhussain/reovault/commits/main'
curl -fsSL --retry 3 --connect-timeout 15 --max-time 90 "$repo_url" -o "$stage_dir/revision.json"
revision="$(python3 - "$stage_dir/revision.json" <<'PY'
import json,re,sys
with open(sys.argv[1]) as handle:
    revision=json.load(handle).get('sha','')
if not re.fullmatch('[0-9a-f]{40}',revision):
    sys.exit('GitHub did not return a valid main-branch revision.')
print(revision)
PY
)"
printf 'Downloading ReoVault from GitHub revision %.12s\n' "$revision"
curl -fsSL --retry 3 --connect-timeout 15 --max-time 180 \
  "https://codeload.github.com/waqaarhussain/reovault/tar.gz/$revision" \
  -o "$stage_dir/source.tar.gz"
mkdir "$stage_dir/source"
tar -xzf "$stage_dir/source.tar.gz" -C "$stage_dir/source" --strip-components=1
for required in install.sh app.py core.py requirements.txt static/vendor/hls.min.js; do
  [[ -s "$stage_dir/source/$required" ]] || { echo "Incomplete download: $required" >&2; exit 1; }
done
# install.sh prompts through the terminal; its stdin is not a curl pipe.
REOVAULT_REVISION="$revision" bash "$stage_dir/source/install.sh" "$@"
