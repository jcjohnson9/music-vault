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
- Treat every pushed release tag as immutable. A corrective release must build
  application code from the original tag, identify later release tooling as
  separate provenance, and must never delete, move, recreate, or force-update
  the tag.
- Never commit runtime or private data.
- Never run a real YouTube sync unless a future batch explicitly authorizes it.
- Preserve the existing working queue behavior unless a batch explicitly
  changes it.
- Preserve canonical cross-source track identity and every source/manual
  playlist origin; source detach, archive, or remote removal must never delete
  personal media or silently erase unrelated membership.
- Treat manual and user-confirmed metadata locks as authoritative; preserve
  provenance/history, and never rewrite audio-file tags without explicit batch
  authorization.
- Existing-library remediation must analyze before apply, auto-apply only
  strict high-confidence unambiguous matches after explicit confirmation, and
  keep unresolved items unchanged. Every media write requires a verified full-
  file backup, temporary-copy writeback, unchanged-audio-payload proof, and
  conflict-aware rollback; reports, caches, artwork, and backups remain private
  runtime data.
- Browser-performance changes must run
  `tools/dev/profile_media_browsers.ps1`; it uses synthetic temporary data only,
  and generated benchmark JSON or screenshots must not be committed.
- A batch that is not primarily visual should use only the minimum sanitized
  visual evidence needed for its acceptance criteria (normally no more than
  about three captures). Add more captures only to diagnose a real rendering
  defect.
- Add targeted tests for new batch behavior without duplicating prior complete
  acceptance matrices. Run the existing full regression suite once as the
  batch gate, and rerun it only when source changes afterward or a failure needs
  correction.
- Music Vault is standalone and has no Watchtower relationship. Prime
  interoperability remains optional and external.
