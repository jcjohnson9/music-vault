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

For Party Mode changes, run the bounded synthetic review harness as well:

```powershell
.\tools\dev\run_party_mode_review.ps1
```

It uses an offscreen Qt platform, temporary synthetic WAV/artwork/metadata, and
no network or personal library. Add `--output .\.ui-review\party-mode` only
when captures are needed for a focused local review, then delete them after the
findings are recorded. Never commit captures, generated audio, benchmark JSON,
or temporary runtime data. See [Developer tools](tools/dev/README.md) and
[Party Mode](docs/PARTY_MODE.md).

Run the read-only public-candidate safety scanner as well:

```powershell
.\tools\dev\pre_public_commit_check.ps1
.\tools\dev\pre_public_history_check.ps1
```

Both scanners must pass. The first checks the tracked/staged publication
candidate; the second checks all local and remote-tracking refs, commits, tags,
annotated-tag messages, historical paths, and bounded reachable objects without
checking out history. Never paste a discovered secret or matching source line
into an issue; report only the affected object/path and remediation type.

Pushed release tags are immutable. A corrective release must build application
code from the original annotated tag and record later release tooling as
separate provenance; never delete, move, recreate, or force-update the tag.

For architecture, release ordering, data handling, and security guidance, read
[Architecture](docs/ARCHITECTURE.md), [Roadmap](docs/ROADMAP.md),
[Data and Privacy](docs/DATA_AND_PRIVACY.md), and [Security](SECURITY.md).
