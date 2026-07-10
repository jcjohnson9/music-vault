# Contributing to Music Vault

Music Vault is a personal project with open-source code. Focused issues and pull
requests are welcome, but no support, review, or response-time SLA is promised.

## Safety and product boundaries

- Never add API keys, credentials, databases, runtime configuration or status
  files, downloaded media, cover caches, archives, logs, private playlists, or
  personal paths.
- Do not submit copyrighted media or artwork with a change.
- Preserve the [authorized-use boundaries](docs/AUTHORIZED_USE.md).
- Keep Music Vault standalone; do not reframe it as Watchtower-dependent.
- Do not change synchronization, queue, playback, database, or runtime-data
  behavior casually. Explain the intended behavior and provide proportionate
  verification for any such proposal.
- Keep changes narrow. Avoid unrelated formatting or broad refactors.

## Development setup

Use Python 3.11 and a project-local `.venv`. Install the development environment
from the project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run source verification before submitting:

```powershell
.\tools\dev\verify.ps1
```

Run the read-only public-candidate safety scanner as well:

```powershell
.\tools\dev\pre_public_commit_check.ps1
```

The scanner must pass without tracked or staged runtime data, media, secrets,
personal paths, or build output. Never paste a discovered secret or matching
source line into an issue; report only the affected file and remediation type.

For architecture, release ordering, data handling, and security guidance, read
[Architecture](docs/ARCHITECTURE.md), [Roadmap](docs/ROADMAP.md),
[Data and Privacy](docs/DATA_AND_PRIVACY.md), and [Security](SECURITY.md).
