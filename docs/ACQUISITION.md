# Acquisition capability (V1.1 development)

Local playback is independent of YouTube and acquisition readiness. The
development application uses one tested capability: yt-dlp 2026.8.19,
yt-dlp-ejs 0.8.0 and Deno 2.9.5, with pinned default Python dependencies.
The official EXE includes Deno and the EJS solver resources; it does not rely
on a desktop shortcut inheriting a developer's PATH. FFmpeg/ffprobe remain
separately provided tools, not bundled command-line executables.

Before acquisition, Music Vault checks versions, verifies solver hashes
against yt-dlp's pinned vendor manifest, and verifies the Windows Deno binary
against the reviewed upstream release. Display checks may be cached; actual
acquisitions revalidate integrity. No runtime pip install, remote EJS download,
browser-cookie access, netrc authentication or automatic updater is enabled.

The Deno Python package's executable was compared with the official Deno
v2.9.5 Windows x64 release. Both have SHA-256
`98f8c2a2d470e4ccb04c935c86ff8050817d877762aec5eaeeb9e409ccb3b9fd`.
The upstream ZIP SHA-256 is
`171efab55ac6b9881fd53ee4c20f8bf3bb1340ffc618483746909014db12216a`.
Dependency changes require reviewed pins, equivalent integrity verification,
focused tests and packaged acceptance; merely bumping a version is insufficient.

## Truthful health and failures

Settings distinguishes a present API key, installed acquisition components and
successful network acquisition. Component readiness does not prove API access,
quota, item availability or a successful media transfer. App Status keeps
schema 1 and adds an identity-free acquisition capability section and sanitized
stage/reason/retry diagnostics. Its legacy `health.ok` indicates the optional
sync prerequisites, not whether local playback is usable.

Failures distinguish readiness, API enumeration, metadata extraction, media
transfer, transformation, final verification and library import. No raw
provider error, signed media URL or credential enters structured diagnostics.
Three consecutive systemic failures across distinct items pause new downloads
for that batch. Deferred items are not marked permanently unavailable, and a
later explicit sync gets a fresh circuit. Full snapshot and existing-membership
reconciliation remain authoritative. Transport/extractor retries and socket
timeouts are finite.

Unverified output is not accepted. The compatibility download archive follows
committed canonical library identities, never download completion alone.
An archive-write failure cannot turn a committed import into a claimed rollback.

## Disposable engineering acceptance

```powershell
.\tools\dev\verify_acquisition.ps1 -Source
.\tools\dev\verify_acquisition.ps1
.\tools\dev\verify_acquisition.ps1 -Mode network
```

Offline is the default. Each run owns a fresh temporary root and checks actual
planner/inspection/import behavior using synthetic media. Network mode is an
explicit, bounded anonymous transfer of the downloader's current open-film
fixture, not a source-playlist sync or personal-library import. The sample is
Big Buck Bunny, (c) 2008 Blender Foundation / www.bigbuckbunny.org,
[CC BY 3.0](https://peach.blender.org/about/); it is removed after verification,
not redistributed. Upstream [replaced its deleted test video](https://github.com/yt-dlp/yt-dlp/pull/17061)
with this fixture. The
official EXE's explicit diagnostic entry dispatches before loading the GUI.
Only aggregate evidence remains; generated media/database payloads are removed.

Unit-test collection is restricted to `tests/`, excluding runtime and build
folders. A Python audit-hook tripwire denies access to the checkout's private
`data/` (apart from public placeholders and ignored engineering reports), and
fails the test session even if application code catches the denial. Synthetic
migration fixtures validate their resolved paths before writing status. This
is not an OS sandbox: packaged/native acceptance still requires a separate
validated temporary runtime.

Public release remains a separate approval gate. The additional runtime and
Python components require an updated binary-license/source-compliance inventory
before any V1.1 publication. Existing v1.0.0 source, tag, artifacts and tagged
release-tooling provenance remain unchanged. Do not bypass the release scanner
or describe a development EXE as a verified public release candidate.

Upstream references: [yt-dlp release](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19),
[EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS),
[Deno release](https://github.com/denoland/deno/releases/tag/v2.9.5).
