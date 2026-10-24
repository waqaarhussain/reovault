# Build validation

Validated on 24 October 2026 with synthetic video and a local RTSP test server. No real camera credentials or footage are included.

- Seven automated tests passed: authentication, session invalidation, CSRF, private camera credentials, camera lifecycle, timeline grouping boundaries, recording gaps, discontinuities, original downloads, oldest-first retention, interrupted-copy reconciliation, recording state persistence, H.264 and H.265 decoding, HEVC hvc1 tagging, and FFmpeg timeout option selection.
- An actual RTSP source through MediaMTX was read by the engine. Standby generated no archive footage; enabling recording created completed archive fragments; stopping recording left live streaming online.
- The same RTSP test passed with the optional compatible H.264 preview enabled.
- Chromium browser checks passed: login, three live video players, archive playback with real decoded frames, hourly versus half-hour grouping, mobile camera form, no JavaScript page errors, and no horizontal overflow at a 390-pixel viewport.
- Desktop and mobile screenshots were inspected during development. Screenshots and synthetic fixture footage are not part of this package.
- JavaScript syntax, Python compilation and shell syntax checks passed.
- All pinned runtime dependencies resolved to Python 3.10-compatible wheels, including the compiled Pydantic dependency. The development runtime itself was Python 3.12, rather than a full Ubuntu 22.04 virtual machine.

Still to validate after installation: your camera stream URLs and secondary TrackMix lens URLs, DDNS and public port forwarding, actual codecs and resolutions, sustained 4K throughput, original-codec playback on your iPhone, and installation/service behaviour on your Ubuntu VPS. These cannot be confirmed from the development environment.

The dashboard intentionally uses HTTP at the VPS IP. Neither HTTP nor ordinary RTSP over TCP encrypts traffic. This follows the requested deployment design.

GitHub deployment additions: four deployment-script tests passed, covering commit-pinned downloads, interactive installer input, invalid revision rejection, code activation, failed-start rollback, preservation of recordings/settings and preservation of another application's updater. Together with the recording-system tests, the suite has eleven passing tests.
