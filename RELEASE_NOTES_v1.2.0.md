<!-- version 1.2.0 -->
# ClipQueue v1.2.0 — Final Release

ClipQueue v1.2.0 is the clean, GitHub-ready release for self-hosted video reviewing, trimming, queuing, and safe output management on Ubuntu with Docker.

## Highlights

- Gallery and carousel-style review flow for host-mounted video folders.
- Slider and numeric controls for precise removal of a selected time range.
- Persistent sequential FFmpeg queue with cancel and clear-queue actions.
- H.264 CRF 23 output with a two-pass size safeguard for oversized results.
- Opening-frame verification to protect against problematic exports.
- Direct gallery deletion and optional source removal only after a valid output is saved.
- Default edit settings: remove `0.0` to `5.0` seconds and delete the original after successful save.
- Custom ClipQueue logo in the portal header and browser tab.
- Portal footer links to SWAKES and GitHub.

## Upgrade note

This final package is intended as a fresh repository release. Existing installations can retain their `.env`, `data`, and `media` folders, but should back them up before replacing project files.
