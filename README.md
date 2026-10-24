# ReoVault

A private camera dashboard and continuous recorder for Ubuntu 22.04. Each camera has its own recording switch. New cameras always start with recording off.

## Install or update from GitHub

```bash
curl -fsSL https://raw.githubusercontent.com/waqaarhussain/reovault/main/bootstrap.sh -o /tmp/reovault-install.sh && sudo bash /tmp/reovault-install.sh
```

If curl is missing, install it first with `sudo apt-get update && sudo apt-get install -y curl`.

The installer asks for a dashboard username/password recording storage allowance and operating mode on first installation. It detects the VPS public IP and prints `http://VPS_IP:8765`. Allow inbound TCP 8765 in the VPS/provider firewall. No domain or certificate is required. The dashboard uses HTTP as requested, so login and video traffic are not encrypted in transit. Native RTSP and the optional ONVIF HTTP connection are also unencrypted.

On subsequent installs, the config, footage and per-camera recording states are preserved. For future GitHub changes, run:

```bash
update-live
```

If another application already owns that command, it is preserved; use `reovault update` instead. The updater builds an isolated code release before switching the current release and restarting the service. A failed startup check restores the previous code/service definition where available. Database migrations are not rolled back. Expect a short recording gap during restart.

## Add cameras and read their actual names

Keep your Reolink NVR in HyBridge mode and retain the current wiring. Reserve each camera's LAN address in eero. All three cameras can keep recording to the Reolink NVR independently.

Give each camera a separate forwarded external **TCP RTSP** port, mapped to its internal RTSP port, normally 554. All dashboard entries may use the same home DDNS hostname. For a direct camera, start with `/Preview_01_main`; some firmware uses `/h264Preview_01_main`. Use the standalone camera credentials, which may differ from the NVR credentials.

To read the **name configured on the camera**, also forward its ONVIF TCP port and enter that external port in the camera form. Reolink's ONVIF port is commonly 8000; check the actual device's port settings rather than assuming. Click **Read device name**, or save the camera with its ONVIF port set. The backend reads ONVIF name scopes and checks the camera's UTC clock. These are read-only operations; it does not change the name, clock, recording schedule, tracking, picture settings or NTP settings.

Example external mappings, replace the private camera addresses and actual internal ports:

| Camera | External RTSP TCP | Internal destination | Optional external ONVIF TCP |
| --- | --- | --- | --- |
| TrackMix front | 8551 | Camera 1 IP:554 | 8851 to Camera 1 ONVIF port |
| TrackMix rear | 8552 | Camera 2 IP:554 | 8852 to Camera 2 ONVIF port |
| E1 Pro | 8553 | Camera 3 IP:554 | 8853 to Camera 3 ONVIF port |

RTSP alone does not reliably expose the device's configured display name. If ONVIF is not reachable or the firmware does not return its name, use a manual name and leave the ONVIF port blank. The helper labels manual names explicitly. It does not pretend a manual label was read from the camera. Different devices with identical names require selection by number.

Port forwarding requires a reachable public home IP. DDNS cannot bypass carrier-grade NAT or an upstream router. Camera HTTPS is not needed by this version. Only enable/forward the protocols actually being used.

In live or both mode, adding a camera starts its **live preview**, with a disposable live buffer. In storage-only mode, a new camera stays disconnected until you enable its recording. It does not start persistent recording, even if every other camera is recording. The dashboard cannot turn recording on; only the local `record` helper can.

## Record individual cameras

```bash
record "Front Driveway"
record "Rear Garden"
```

Names containing spaces can be quoted. Unquoted words are also joined into a name. `record` by itself presents the known camera names and lets you choose one by name or number. It does not enable every camera.

```bash
record stop "Front Driveway"
record status
```

Only the selected camera changes. Existing enabled cameras remain enabled after restart/update, and future cameras remain off. Upgrading from the first version migrates the former global recording switch to the cameras that already existed, once. Cameras added after migration never inherit that switch.

`record status` shows each switch and its capture-time source. A switch can be on while the camera is offline or waiting for verified timestamps; that does not mean footage is being archived yet. The dashboard reports that state.

## Choose what the VPS does

Run `reovault mode` for an interactive menu, or select directly:

```bash
reovault mode storage
reovault mode live
reovault mode both
reovault mode status
```

| Mode | Continuous storage | Live view | Compatible preview conversion |
| --- | --- | --- | --- |
| `storage` | Cameras enabled with `record` | Disabled | Disabled |
| `live` | Paused for every camera | Enabled | Only cameras configured for it |
| `both` | Cameras enabled with `record` | Enabled | Only cameras configured for it |

Your camera recording switches are preserved when changing mode. Returning from live-only to storage/both resumes previously enabled cameras; new cameras remain off. `record` refuses to start archiving in live-only mode and tells you to change mode first. Use `record stop "Camera Name"` to clear a camera's saved choice. Mode changes may briefly restart camera connections. Previously stored footage stays available for playback/export in every mode.

Storage-only still receives the original camera stream and uses a short disposable fragment buffer internally, but it does not serve live video or run the optional H.264 preview conversion. Cameras with recording off have no RTSP worker in that mode. Live-only keeps its disposable preview buffer without archiving it. Passing through original 4K video uses far less CPU than converting it; storage and network traffic still need enough capacity.

## Connection outages and hourly files

The supervisor keeps trying each required camera until it reconnects. Attempts have bounded connection/read timeouts, with retries after failed or stale sessions. Recording switches remain enabled throughout an outage. After the camera returns and its capture timestamps verify, recording resumes automatically without another `record` command. Retry delays and the next keyframe mean recovery is not instantaneous.

Every reconnect starts a separate file. Files also split at clock-hour changes using camera capture timestamps in the configured timezone (Europe/London by default). For example, an outage from 20:20 to 20:30 produces one file covering the available footage before 20:20, another beginning after reconnection near 20:30, and a new file at the 21:00 hour boundary. A file can therefore be shorter than an hour. Keyframe/fragment alignment can put the split a few seconds after the exact hour; original encoded video is preserved with no boundary re-encoding. UTC offsets in file names distinguish repeated hours when daylight saving ends.

No blank video is inserted into the gap, and missing footage is not fetched from the NVR. Timeline gaps remain visible. Any NVR footage recorded locally during an internet outage remains independent of this VPS recorder.

## Camera time, live time and export time

RTSP ingestion uses PyAV with its bundled modern FFmpeg libraries. Encoded H.264/H.265 packets are remuxed without video re-encoding. AAC source audio is copied. If another source audio codec is encountered, the dashboard explicitly reports video-only capture for this version.

The camera's **RTP/RTCP sender reference** supplies the mapping from video presentation timestamps to UTC. The recorder reads that reference, then reads the actual video sample presentation time in each MP4 fragment. The live manifest, stored index, timeline, playback, file names and range-export selection use that same mapping. It does not label server arrival time as camera capture time. The compatible H.264 live preview retains source timestamps and receives the same camera-time mapping; its conversion does not alter the original stored video.

The live tile displays the capture time of the frames actually being played, so a buffered live view is not labelled with the current page clock. Times are displayed in **Europe/London**, including daylight-saving changes. The helper uses the same timezone. The original camera's burned-in timestamp, if enabled on the device, remains part of the original picture.

If the stream does **not** provide a valid camera sender reference, live preview can still work, but persistent archiving waits and the dashboard explicitly says that camera time is unverified. It does not silently substitute server time. A capture-clock discontinuity causes a new stream session instead of rewriting old timestamps. ONVIF clock offsets over five seconds produce a warning; camera and server clocks are not automatically changed.

Physical Reolink firmware must still be tested to confirm that it supplies the expected sender references. A wrong camera clock or incorrectly configured camera overlay timezone remains wrong until corrected on the camera. UTC capture references are rendered using the app's stated timezone. This implementation cannot promise that an incorrectly configured burned-in clock will match it. Existing footage from the earlier version retains its original, server-based index and is not retroactively marked camera-synchronized.

## Recording files and Termius SFTP

New continuous recording files are independently playable fragmented MP4 files, stored as:

```text
/Reolink/Camera Name/2026-10-24_19-57-00+0100_hour_session_id.mp4
```

Original fragments are small internal chunks, but dashboard playback groups them into **1 hour, 30 minutes, 15 minutes or 5 minutes** with a daily timeline, gaps and click-to-seek. Several indexed fragments share one hourly file, which includes initialization data and can be played independently, including while it is still growing. Internal fragment indexes keep timeline playback and range exports precise without duplicating video storage. A browser segment download extracts just that indexed fragment into `Saves`. Video remains in its original codec and resolution.

Manually saved clips are separate:

```text
/Reolink/Camera Name/Saves/2026-10-24_19-57-00+0100_to_2026-10-24_20-10-00+0100_id.mp4
```

A root SSH/SFTP login also gets `/root/Reolink` as a shortcut when that path is free. If you log in to Termius as root and start in `/root`, open `Reolink` there. You can also browse `/Reolink` from the filesystem root. The installer does not replace an existing `/root/Reolink` file/folder.

For a non-root sudo user, the installer adds the installing user to a separate `reolink-media` group that can read the media. Reconnect the SSH/SFTP session for new group membership to apply. This group does not grant access to the private camera credential database. If installation was performed directly as root, only root and the recording service get media access by default.

Camera folders are assigned from the camera's known name when recording or saving first begins. They stay stable if a label is later changed, so old files are not moved or split unexpectedly. Duplicate or unsafe names are given safe, unique folders. Do not put unrelated files in the managed continuous recording area. Existing older recordings remain in their previous private archive and can still be viewed or exported.

## Guided download helper

Run in the Termius **SSH terminal**:

```bash
download
```

It asks:

1. Camera name or number, using the names already read/stored by the app.
2. Date in `DD/MM`, without the year. Blank means today; `yesterday` is also accepted. The current year is used and the timezone is printed.
3. Start and end time, such as `0757pm - 08:10pm`.

`19:57 - 20:10`, `1957 - 2010` and mixed colon/no-colon 12-hour formats also work. An end time earlier than the start means the following day. If a time occurs twice when clocks go back, the helper asks which occurrence; nonexistent clock-change times are rejected.

The example is 13 minutes: 19:57 to 20:00 is 3 minutes, plus 10 minutes to 20:10. The source video is copied into one MP4 in `Saves`, with a JSON sidecar describing the requested camera-time range and timing source. The helper prints the exact VPS path. **Refresh Termius SFTP, then use Termius to download the file to your phone/computer.** An SSH command cannot force the SFTP app to save a file onto your local device.

Exports preserve original encoded video. They do not silently switch to a lower-resolution stream or re-encode. A keyframe-aligned cut can include a little footage before/after the requested boundary. A frame-exact arbitrary cut would require re-encoding at least the boundary frames; this helper deliberately reports the original-quality boundary limitation instead. The helper refuses intervals with missing footage or incompatible source format changes rather than hiding gaps or stitching incompatible formats.

Clicking an individual segment's **Download original** link in the dashboard also saves a persistent copy in that camera's `Saves` folder before returning it to the browser.

## Retention, storage and maintenance

Oldest continuous footage is deleted first at the configured recording allowance or free-space reserve. New hourly files rotate as whole files, including all their fragment index entries; older per-fragment files remain supported. If retention removes an active file, the next fragment begins a new file. `Saves` clips are **not automatically deleted** by recording rotation. They consume disk space, which the free-space reserve checks. Delete saved clips manually through SFTP when they are no longer needed.

Exports pin their input fragments while FFmpeg reads them, so retention cannot remove the inputs halfway through a job. Only one range export runs at a time. If the service restarts during an export, rerun the helper. A suddenly interrupted export may leave a hidden `.export-*` working directory in `Saves`; it contains temporary inputs, not a completed saved clip.

Application code is in `/opt/reovault/releases`, selected by `/opt/reovault/current`. Private settings are in `/etc/reovault/config.json`; the database and disposable live cache are in `/var/lib/reovault`. New recordings and saved clips are in `/Reolink`. If using a separate media disk, mount it persistently at `/Reolink` before installation. A missing or full media mount must be corrected before recording can continue reliably.

```bash
reovault status
reovault logs
reovault restart
reovault password
reovault version
update-live
```

No existing NVR history is imported, no internet-outage gaps are recovered, and this version does not implement PTZ controls, event detection or notifications. Original HEVC playback depends on the viewing device. Compatible live viewing converts only the preview to H.264 up to 1080p; saved HEVC recordings still need a player that supports that codec.

## Validation

```bash
python3 -m venv .test-venv
.test-venv/bin/pip install -r requirements.txt pytest httpx
.test-venv/bin/python -m pytest tests -q
```

The suite covers per-camera recording isolation, new-camera defaults, one-time migration, date/time formats, DST ambiguity, packet clock references, actual H.264/H.265 fragments and exports, original-codec playback, `Saves` preservation, timelines, authentication and deployment rollback. Local RTSP and browser tests use synthetic footage. Physical cameras, forwarding and sustained 4K operation still need testing on your network.

hls.js is bundled under its license in `static/vendor/hls.LICENSE.txt`. PyAV is installed from its published Python wheel. No camera credentials or footage are committed to GitHub.
