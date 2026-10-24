#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then exec sudo bash "$0" "$@"; fi
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 22.04 ]]; then
  echo 'This installer targets Ubuntu 22.04.' >&2
  exit 1
fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --update ) ]]; then
  echo 'Usage: install.sh [--update]' >&2
  exit 1
fi
if [[ "${1:-}" == --update && ! -f /etc/reovault/config.json ]]; then
  echo 'No installed configuration found. Run the bootstrap command first.' >&2
  exit 1
fi
src_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for required in app.py core.py capture.py media_time.py device.py exporter.py clip_input.py requirements.txt manage.py bootstrap.sh update-live.sh static/vendor/hls.min.js; do
  [[ -s "$src_dir/$required" ]] || { echo "Missing package file: $required" >&2; exit 1; }
done
revision="${REOVAULT_REVISION:-local}"
[[ "$revision" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo 'Invalid revision identifier.' >&2; exit 1; }
umask 022
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -o DPkg::Lock::Timeout=600 install -y python3 python3-venv python3-pip ffmpeg rsync ca-certificates curl
id reovault >/dev/null 2>&1 || useradd --system --home-dir /var/lib/reovault --shell /usr/sbin/nologin reovault
install -d -m 0755 /opt/reovault /opt/reovault/releases
install -d -m 0750 -o reovault -g reovault /var/lib/reovault
install -d -m 0750 -o root -g reovault /etc/reovault
getent group reolink-media >/dev/null || groupadd --system reolink-media
install -d -m 2750 -o reovault -g reolink-media /Reolink
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then usermod -a -G reolink-media "$SUDO_USER"; fi
if [[ ! -e /root/Reolink && ! -L /root/Reolink ]]; then ln -s /Reolink /root/Reolink; fi
release_dir="$(mktemp -d "/opt/reovault/releases/${revision:0:12}.XXXXXX")"
chmod 0755 "$release_dir"
backup_dir="$(mktemp -d /tmp/reovault-activate.XXXXXX)"
previous_release="$(readlink -f /opt/reovault/current 2>/dev/null || true)"
[[ ! -f /etc/systemd/system/reovault.service ]] || cp /etc/systemd/system/reovault.service "$backup_dir/reovault.service"
activated=0
complete=0
rollback() {
  result=$?
  trap - EXIT
  if [[ $complete -eq 0 ]]; then
    if [[ $activated -eq 1 ]]; then
      echo 'New release failed its startup check. Restoring the previous service.' >&2
      if [[ -n "$previous_release" && -d "$previous_release" ]]; then
        ln -s "$previous_release" /opt/reovault/.current-rollback
        mv -Tf /opt/reovault/.current-rollback /opt/reovault/current
      else
        rm -f /opt/reovault/current
      fi
      if [[ -f "$backup_dir/reovault.service" ]]; then
        cp "$backup_dir/reovault.service" /etc/systemd/system/reovault.service
        systemctl daemon-reload || true
        systemctl restart reovault || true
      else
        systemctl stop reovault || true
        rm -f /etc/systemd/system/reovault.service
        systemctl daemon-reload || true
      fi
    else
      echo 'Preparation failed. The running application has not been switched.' >&2
    fi
    # Only remove this unactivated code release, never recordings or configuration.
    if [[ $activated -eq 0 ]]; then rm -rf -- "$release_dir"; fi
  fi
  rm -rf -- "$backup_dir"
  exit "$result"
}
trap rollback EXIT
# Bootstrap extraction may be private (0700). Never inherit that mode into service code.
rsync -a --chmod=D755,F644 --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='__pycache__' \
  --exclude='.pytest_cache' --exclude='*.sqlite*' --exclude='test-data' "$src_dir/" "$release_dir/"
chmod 0755 "$release_dir" "$release_dir/"*.sh
# The environment is built at its final path; venv launchers contain absolute paths.
# The current application keeps running while the next environment is prepared.
python3 -m venv "$release_dir/venv"
"$release_dir/venv/bin/pip" install --disable-pip-version-check -r "$release_dir/requirements.txt"
"$release_dir/venv/bin/python" -m compileall -q "$release_dir/app.py" "$release_dir/core.py" "$release_dir/manage.py"
REOVAULT_RECORDINGS=/Reolink "$release_dir/venv/bin/python" "$release_dir/manage.py" setup
chown root:reovault /etc/reovault/config.json
chmod 0640 /etc/reovault/config.json
# Initial setup creates the DB as root. Existing video trees are never recursively changed.
chown reovault:reovault /var/lib/reovault /var/lib/reovault/archive /var/lib/reovault/live
find /var/lib/reovault -maxdepth 1 -type f -name 'index.sqlite*' -exec chown reovault:reovault {} +
printf '%s\n' "$revision" > "$release_dir/REVISION"
cat > "$backup_dir/new-service" <<'UNIT'
[Unit]
Description=ReoVault camera streaming and recording
After=network-online.target
Wants=network-online.target
[Service]
User=reovault
Group=reovault
SupplementaryGroups=reolink-media
WorkingDirectory=/opt/reovault/current
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=REOVAULT_CONFIG=/etc/reovault/config.json
Environment=REOVAULT_DATA=/var/lib/reovault
Environment=REOVAULT_RECORDINGS=/Reolink
ExecStart=/opt/reovault/current/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8765 --no-proxy-headers --no-access-log
Restart=on-failure
RestartSec=5
TimeoutStopSec=90
KillMode=mixed
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/reovault /Reolink
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
[Install]
WantedBy=multi-user.target
UNIT
# Atomically switch code only after dependencies and setup have succeeded.
link_path="/opt/reovault/.current-$(basename "$release_dir")"
ln -s "$release_dir" "$link_path"
mv -Tf "$link_path" /opt/reovault/current
activated=1
install -m 0644 "$backup_dir/new-service" /etc/systemd/system/reovault.service
systemctl daemon-reload
systemctl enable reovault
systemctl restart reovault
healthy=0
for attempt in {1..30}; do
  if systemctl is-active --quiet reovault && curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8765/ -o /dev/null; then
    healthy=1
    break
  fi
  sleep 1
done
if [[ $healthy -ne 1 ]]; then
  journalctl -u reovault -n 30 --no-pager >&2 || true
  exit 1
fi
# Install stable wrappers after the new release passes its health check.
cat > /usr/local/bin/record <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then exec sudo /usr/local/bin/record "$@"; fi
exec env REOVAULT_RECORDINGS=/Reolink /opt/reovault/current/venv/bin/python /opt/reovault/current/manage.py "$@"
WRAPPER
cat > /usr/local/bin/download <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then exec sudo /usr/local/bin/download "$@"; fi
exec env REOVAULT_RECORDINGS=/Reolink /opt/reovault/current/venv/bin/python /opt/reovault/current/manage.py download
WRAPPER
cat > /usr/local/bin/reovault <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then exec sudo /usr/local/bin/reovault "$@"; fi
case "${1:-status}" in
  status) systemctl status reovault --no-pager ;;
  restart) systemctl restart reovault ;;
  logs) journalctl -u reovault -n 80 --no-pager ;;
  update) exec bash /opt/reovault/current/update-live.sh ;;
  version) cat /opt/reovault/current/REVISION ;;
  mode) shift; exec /opt/reovault/current/venv/bin/python /opt/reovault/current/manage.py mode "$@" ;;
  password) /opt/reovault/current/venv/bin/python /opt/reovault/current/manage.py password
            chown root:reovault /etc/reovault/config.json
            systemctl restart reovault ;;
  *) echo 'Usage: reovault [status|restart|logs|password|update|version|mode]' >&2; exit 1 ;;
esac
WRAPPER
chmod 0755 /usr/local/bin/record /usr/local/bin/download /usr/local/bin/reovault
existing_updater="$(command -v update-live || true)"
if [[ -z "$existing_updater" ]] || grep -qF '# reovault-update-live' "$existing_updater" 2>/dev/null; then
  install -m 0755 "$release_dir/update-live.sh" /usr/local/bin/update-live
else
  printf '\nExisting update-live belongs to another application: %s\n' "$existing_updater"
  echo 'Preserved it. Use reovault update for this application.'
fi
complete=1
server_ip="$(curl -4fsS --connect-timeout 3 --max-time 5 https://api.ipify.org || true)"
if [[ ! "$server_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  server_ip="$(hostname -I | awk '{print $1}')"
fi
server_ip="${server_ip:-YOUR_VPS_IP}"
printf '\nReoVault ready. Revision: %.12s\nDashboard: http://%s:8765\n' "$revision" "$server_ip"
cat <<'DONE'

Sign in with the dashboard credentials you chose, then add your cameras.
Allow inbound TCP 8765 in your VPS/provider firewall if needed.
No firewall rules were changed automatically.

Start one camera:  record "Camera Name"
Stop one camera:   record stop "Camera Name"
Export a clip:     download
Recording status: record status
Choose mode:      reovault mode
Future updates:   update-live  (or reovault update)

Every newly added camera starts OFF. Recording files: /Reolink/Camera Name/.
Saved clips: /Reolink/Camera Name/Saves/. Refresh Termius SFTP to see them.
A root login also has /root/Reolink as a shortcut.
Updates preserve existing per-camera recording state,
cameras, credentials, storage settings and footage. The service restarts
briefly after the new code is ready, so expect a short recording gap.
Dashboard uses HTTP as requested; login and video traffic are not encrypted.
DONE
