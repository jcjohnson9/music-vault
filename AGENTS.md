# Music Vault batch delivery rules

- A Music Vault batch is one complete delivery unit.
- Every normal code-changing batch includes implementation, tests, source
  verification, publication safety scanning, Python compilation, an official
  EXE rebuild, safe packaged startup validation where appropriate,
  documentation, a feature-branch commit and push, merge to `main` after all
  automated gates pass, push of `main`, feature-branch cleanup, and a final
  report.
- Do not split normal implementation, verification, building, validation, and
  merge work into artificial `.1`, `.2`, or `.3` sub-batches.
- Use a numbered corrective sub-batch only for a real defect, failed acceptance
  criterion, incomplete prompt delivery, or blocked batch closeout.
- Codex owns routine tests, verification, builds, safety scans, Git commands,
  and status checks. Do not ask Jeremy to run routine engineering commands.
- Ask Jeremy only to judge visual quality, subjective UX, audible behavior,
  hardware-dependent behavior, or behavior that cannot be automated reliably.
- Rebuild the official application after every code-changing batch. Its target
  is `dist\MusicVault\MusicVault.exe`; an isolated alternate build does not
  count when the normal desktop target is stale.
- Never merge a feature branch before all required automated gates pass.
- Never commit runtime or private data.
- Never run a real YouTube sync unless a future batch explicitly authorizes it.
- Preserve the existing working queue behavior unless a batch explicitly
  changes it.
- Browser-performance changes must run
  `tools/dev/profile_media_browsers.ps1`; it uses synthetic temporary data only,
  and generated benchmark JSON or screenshots must not be committed.
- Music Vault is standalone and has no Watchtower relationship. Prime
  interoperability remains optional and external.
