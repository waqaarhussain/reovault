#!/usr/bin/env bash
set -euo pipefail
release_dir="${1:?Pass the release directory}"
case "$(dpkg --print-architecture)" in
  amd64) rtc_arch=amd64; rtc_sha=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6 ;;
  arm64) rtc_arch=arm64; rtc_sha=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50 ;;
  *) echo 'WebRTC supports amd64 and arm64 on this installer.' >&2; exit 1 ;;
esac
install -d -m 0755 "$release_dir/bin"
rtc_cached=/opt/reovault/current/bin/go2rtc
if [[ -f "$rtc_cached" ]] && [[ "$(sha256sum "$rtc_cached" | cut -d ' ' -f 1)" == "$rtc_sha" ]]; then
  cp "$rtc_cached" "$release_dir/bin/go2rtc.download"
else
curl -fsSL --retry 3 --connect-timeout 15 --max-time 180 \
  "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_linux_${rtc_arch}" \
  -o "$release_dir/bin/go2rtc.download"
fi
printf '%s  %s\n' "$rtc_sha" "$release_dir/bin/go2rtc.download" | sha256sum -c -
chmod 0755 "$release_dir/bin/go2rtc.download"
mv "$release_dir/bin/go2rtc.download" "$release_dir/bin/go2rtc"
