<!-- version 1.2.0 -->
# Changelog

All notable ClipQueue changes are recorded here.

## [1.2.0] - Final GitHub release

- Added a persistent portal footer with links to [SWAKES](https://www.swakes.co.uk) and the [ClipQueue GitHub repository](https://github.com/pqpxo/clipqueue).
- Bundled the supplied ClipQueue brand logo for the web header and browser icon.
- Consolidated all previous application updates into one clean, GitHub-ready source package.
- Added a first-run setup script, Git ignore rules, placeholder media/data folders, release notes, and issue templates.
- Corrected the application-reported version to `1.2.0`.

## [1.1.4]

- Replaced the previous header mark with the supplied ClipQueue logo.

## [1.1.3]

- Defaulted new edits to remove `0.0`–`5.0` seconds.
- Enabled **Delete original after successful save** by default.

## [1.1.2]

- Added **Clear queue** control.

## [1.1.1]

- Changed the output-size fallback to two-pass H.264 encoding.
- Added opening-frame decode verification to avoid corrupted/pixelated starts.

## [1.1.0]

- Changed standard MP4 encoding to CRF 23 with AAC 128 kbps.
- Added a source-size safeguard and source deletion options.
