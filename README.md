# ReoVault

A self-hosted camera dashboard and continuous recorder for Ubuntu 22.04.

## What this version does

- Dark desktop and mobile dashboard with live camera tiles, full screen and audio.
- Direct camera RTSP over TCP using a DDNS hostname and one forwarded port per camera.
- One persistent source stream per configured entry, shared by every live viewer and the recorder.
- `record` starts continuous recording. `record stop` stops archiving but leaves live view running.
- Recording state survives service restarts and server reboots.
- A full-day playback timeline with recorded coverage, visible gaps and click-to-seek.
- Group recordings into **1 hour, 30 minutes, 15 minutes or 5 minutes**. Each block plays continuously across the stored fragments.
- Dates and time labels use the viewing device's local timezone, including daylight-saving days.
- Oldest completed recording fragments are automatically deleted at the configured allowance or free-space reserve.
- Original encoded video is copied, with no resolution or bitrate change. Audio is converted to AAC for playback compatibility.
- Optional compatible live preview uses H.264, up to 1080p, without changing the original recording.
- Login, session expiry, CSRF protection, credential-safe camera responses and original segment downloads.

This version records new incoming footage. It does not import NVR history, fill internet outage gaps, operate PTZ, send motion notifications or provide person detection. It does not use reostream, which is an optional future transport if native RTSP proves unreliable. Nothing changes the Reolink recording schedules, tracking or picture settings.

## Install from GitHub

Repository: https://github.com/waqaarhussain/reovault

Run this single command on the Ubuntu VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/waqaarhussain/reovault/main/bootstrap.sh -o /tmp/reovault-install.sh && sudo bash /tmp/reovault-install.sh
```

The public repository contains application code only. Your camera credentials, dashboard password, settings, recording index and footage stay on the VPS. They are not uploaded to GitHub.

The bootstrap resolves the current `main` commit and downloads its immutable archive, so all application files come from the same commit. It then launches the installer with terminal input available for the first setup prompts.

If curl is missing on a minimal Ubuntu installation, run `sudo apt-get update && sudo apt-get install -y curl` first.

The installer targets Ubuntu **22.04**, uses the system Python 3.10 and FFmpeg, creates a dedicated `reovault` service account, and installs versioned code releases under `/opt/reovault/releases`, selected by `/opt/reovault/current`. Configuration is `/etc/reovault/config.json`; video and the index are under `/var/lib/reovault`.

If using a separate recording disk, mount it at `/var/lib/reovault` **before installation** and ensure that mount is persistent. Do not put unrelated files inside the `archive` or `live` subdirectories. The recorder manages those directories. The storage allowance applies to recordings, while the free-space reserve also protects room for the live cache and other system files. Never allocate the full disk to recordings.

Choose a dashboard username, a password of at least 12 characters and the storage allowance when prompted. The installer detects the VPS public IPv4 and prints:

```text
http://YOUR_VPS_IP:8765
```

If the external IP lookup is unavailable, it uses a local address; on a NAT-hosted VPS replace that with your provider's public IP. Allow inbound **TCP 8765** on the VPS firewall and provider firewall. The installer does not change firewall rules. It does not require a domain name, a certificate, Docker, ONVIF or camera HTTPS access.

The dashboard intentionally uses **HTTP**, as requested. Dashboard credentials, sessions and video are not encrypted in transit. Native RTSP over TCP is also unencrypted. Strong passwords do not provide transport encryption. Do not assume a nonstandard port makes a camera private. Restrict access at network firewalls where available; HTTPS on a camera does not encrypt an independent RTSP connection.

## eero and Reolink setup

Your NVR's **HyBridge mode** already gives the TrackMix cameras router-assigned addresses. Keep that wiring. The E1 Pro remains on Wi-Fi and can keep recording to the NVR.

1. Reserve each camera's current LAN address in eero so forwarding stays valid.
2. Confirm RTSP is enabled. If disabled, enabling it is the only camera protocol change needed.
3. Forward distinct **TCP** external ports to each camera's internal RTSP port (usually 554).

Example forwarding, replace the private addresses with your actual camera addresses:

| Camera | External TCP port | Destination | RTSP path in dashboard |
| --- | --- | --- | --- |
| TrackMix front | 8551 | Camera 1 IP, TCP 554 | `/Preview_01_main` |
| TrackMix rear | 8552 | Camera 2 IP, TCP 554 | `/Preview_01_main` |
| E1 Pro | 8553 | Camera 3 IP, TCP 554 | `/Preview_01_main` |

All entries use your **home eero DDNS hostname**, not the VPS IP. Use each standalone camera's credentials, which may differ from the NVR credentials. Supply hostname, external port, username, password and stream path separately. Reserved characters in passwords are safely encoded by the server. Passwords are not embedded into the web page or returned in API responses.

Do not forward ONVIF or camera HTTPS for this version. RTSP over TCP carries video and audio through the same forwarded TCP connection. Forwarding additional services is unnecessary.

Some firmware uses `/h264Preview_01_main` instead. Test `/Preview_01_main` first. A direct camera's channel is normally 01 for every camera, because the port chooses the device. NVR channel numbering is different.

TrackMix has a separate tracking view. The exact secondary stream URL needs checking against your camera firmware; do not assume a second lens has the same 4K resolution. Add it as another entry once its URL is verified. It increases upload, storage and camera stream usage. This version defaults to one original main stream per camera.

Port forwarding requires a reachable public home IP. DDNS alone cannot bypass carrier-grade NAT or an upstream router. Verify the feed from outside your home network, preferably from the Ubuntu server. An ffprobe connection briefly opens during codec discovery, closes, and is followed by the persistent FFmpeg connection.

## Start recording

Add your cameras in the dashboard and check they are online, then run:

```bash
record
```

Other controls:

```bash
record stop
record status
reovault status
reovault restart
reovault logs
reovault password
update-live
reovault update
reovault version
```

`record` may ask for your Ubuntu sudo password. This authorizes the local helper to read its private control token. Camera passwords are never typed into a shell command.

On a fresh install, recording is off. Starting records the next completed fragment that overlaps the start time, so the first clip may contain a few seconds before the command. The UI updates as fragments finish. Internal fragments target four seconds but the camera's keyframe interval determines actual duration. These are implementation details: playback is grouped into the selected hour/half-hour/quarter-hour/five-minute blocks.

Stopping does not delete footage. Starting again does not backfill the period when recording was stopped. Internet outages create visible gaps. Streams reconnect automatically, but this version cannot recover footage missed during an outage.

## Timeline and playback

Open **Playback**, choose a camera and date, then pick the grouping. Click a block to play its complete available footage. Click the daily timeline to start at a particular point within that block. Empty periods are shown as gaps. An unavailable moment produces a message rather than silently playing a different block. Player controls seek within the chosen block. Times shown under the player follow the original recording timestamps.

A selected block includes whole keyframe-aligned fragments at its edges, which can extend a few seconds outside its nominal time bounds. The daily timeline preserves those wall-clock timestamps and does not invent missing footage.

Individual original fragment downloads are available under the expandable segment list. Downloads are fragmented MP4 files with the required initialization data included; they preserve the original video codec. This version does not yet export an entire hour as a single MP4 file.

TrackMix main video is commonly HEVC/H.265. Live and recorded HEVC playback depends on the browser and hardware decoder. On a device that cannot play HEVC, enable **compatible live viewing** for that camera. This converts only the live preview using server CPU. Original HEVC recordings still require a compatible player; download a segment and use VLC if the browser cannot play it. This does not silently lower stored quality.

## Rotation and restarts

The retention worker removes the oldest completed archive segments across all cameras until the recording allowance and minimum free space are satisfied. Camera removal preserves old footage for playback and normal rotation. Reducing the allowance can immediately delete old footage. There is no recycle bin.

Recent footage still in the live buffer is not part of persistent recording until archived. A sudden server power loss can lose a segment being written or copied. Completed, indexed archive segments survive restarts. Atomic file replacement and startup reconciliation handle interrupted copies. The live buffer is disposable and is recreated after restart.

The free-space reserve is not a guarantee against an unrelated program filling the filesystem between checks. Engine warnings appear in the dashboard if storage or recording processing fails.

## Updates and maintenance

After changes are committed to GitHub, run:

```bash
update-live
```

The updater fetches the latest bootstrap, resolves the latest `main` commit, and builds the new code and Python environment in a separate release directory while the old version keeps running. It switches the `current` symlink after preparation, restarts the service, and checks that the dashboard responds. If activation fails, it restores the previous code target and service definition where available. This is an application rollback, not a database migration rollback.

The camera entries, credentials, footage, index, storage allowance and recording switch are stored separately and are preserved. There is a short recording gap when the service restarts. The installer also supports upgrading a previous ZIP installation.

If this VPS already has another application's `update-live` command, the installer preserves it and prints that fact. Use `reovault update` for ReoVault in that case. Both commands fetch updates from this dedicated repository, not from any other project.

Run `reovault version` to see the deployed Git commit. Older code releases are retained for recovery. Do not run two app workers or service instances against the same recording directory.

Camera credentials are stored in a private SQLite database, protected by service-account file permissions. The dashboard password is salted and scrypt-hashed. Server administrators can inspect camera credentials, including FFmpeg process arguments, so this is not designed for a shared untrusted shell host. Do not publish `/etc/reovault` or `/var/lib/reovault` in a public repository.

If a camera never connects, verify DDNS resolution, public IP reachability, TCP forwarding, RTSP enablement and standalone camera credentials. FFmpeg error logs deliberately do not persist input URLs because those can include credentials. The UI exposes a generic connection error instead.

## Validation

The included tests generate real H.264 and H.265 footage with FFmpeg, build live HLS fragments, archive them, verify original fragment bytes, decode downloaded MP4s, check HEVC `hvc1` tagging, test timeline boundaries and gaps, exercise session/CSRF protection, and verify oldest-first rotation and restart state.

```bash
python3 -m venv .test-venv
.test-venv/bin/pip install -r requirements.txt pytest httpx
.test-venv/bin/python -m pytest tests -q
```

Your physical Reolink devices, DDNS forwarding, sustained 4K performance and installation on your VPS still require testing on your network. The build environment is not your Ubuntu VPS.

## Technical references

- Reolink HyBridge: https://support.reolink.com/articles/37372221001625-Introduction-to-Reolink-NVR-HyBridge-Mode/
- Reolink RTSP: https://support.reolink.com/articles/900000630706-Introduction-to-RTSP/
- FFmpeg HLS muxer: https://ffmpeg.org/ffmpeg-formats.html#hls-2
- hls.js: https://github.com/video-dev/hls.js

hls.js 1.6.19 is bundled, so the installed dashboard does not require a third-party script CDN. Its license is in `static/vendor/hls.LICENSE.txt`.
