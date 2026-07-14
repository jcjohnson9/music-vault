# Party Mode

Party Mode is Music Vault's optional full-screen now-playing experience on the
v1.1.0 development line. The latest public stable release remains v1.0.0; no
public v1.1.0 release has been created.

## Entering and leaving

Use the Party Mode button in the player bar or press `F11`. Music Vault opens a
separate full-screen window on the display containing the main window. Press
`Escape` or `F11`, or use **Exit Full Screen**, to return. Opening or closing
Party Mode does not pause, restart, seek, or replace the current track.

Party Mode reuses the existing media player, audio output, queue, base playback
context, now-playing identity, volume, Auto, Shuffle, and Repeat state. It never
creates a second player. Closing the main application also closes Party Mode.

## Presets

- **Pulse** emphasizes album artwork, a restrained reactive halo, and orbiting
  particles.
- **Starfield** adds depth-like movement whose speed follows energy while the
  artwork remains visible.
- **Aurora** uses smooth frequency-led ribbons with particles in a secondary
  role.

Press `V` or use the overlay preset control to cycle presets. Palette and preset
changes transition smoothly, without white flashes or unrelated rainbow
cycling. The selected preset persists in local configuration.

## Controls and shortcuts

The control overlay starts visible, reappears on pointer or keyboard activity,
and can auto-hide after the configured timeout. Hovering over its controls keeps
it visible. Press `H` to toggle it manually and `?` for shortcut help.

| Key | Action |
| --- | --- |
| `Escape`, `F11` | Exit Party Mode |
| `Space` | Play or pause |
| `Left`, `Right` | Seek backward or forward 10 seconds |
| `Ctrl+Left`, `Ctrl+Right` | Previous or next track |
| `Up`, `Down` | Raise or lower volume by 5 |
| `M` | Mute or restore volume |
| `V` | Cycle the visual preset |
| `H` | Toggle the controls overlay |
| `S`, `A`, `R` | Toggle Shuffle, Auto, or cycle Repeat using existing rules |
| `?` | Show shortcut help |

Text-entry controls retain their normal key handling. Volume and seeking remain
bounded, Auto and Shuffle remain mutually exclusive, and existing Repeat and
manual-queue priorities do not change.

## Audio-reactive behavior and fallback

On a compatible Qt multimedia backend, Music Vault attaches decoded-buffer
output to the existing media player while preserving its existing audio output.
A bounded analyser converts a small recent PCM window to aggregate RMS, peak,
frequency-band, and beat features. It processes only the latest work at a
limited rate; stale buffers are dropped rather than accumulated.

Some backend, codec, paused, stopped, or error states do not provide decoded
buffers. Party Mode then uses a calm ambient fallback based on playback timing
and state. The interface does not claim that procedural fallback motion is real
audio analysis, and the absence of reactive data never produces a modal error.

## Privacy

Party Mode requires no network access and makes no provider request. Decoded
samples are processed transiently in memory only. Music Vault does not record
PCM, write decoded audio to disk, retain `QAudioBuffer` objects, send audio to a
service, or add samples, spectra, artwork pixels, monitor identity, or screen
coordinates to App Status. Artwork is sampled locally only to derive a cached
palette; the artwork file is never modified.

The developer review tool is intentionally different: it creates a short,
fully synthetic WAV inside an operating-system temporary directory so the
review is repeatable without personal media. That complete temporary runtime is
removed when the tool exits.

## Visual safety and accessibility

The renderer caps brightness, particle speed, beat response, artwork scale, and
frame delta. It avoids rapid full-screen flashes, alternating strobe patterns,
hard color jumps, and bright white transitions. Background tones remain dark
enough for readable metadata and controls.

Reduced-motion mode substantially lowers particle count, movement, pulses, and
transition travel while preserving a composed ambient display. With no active
track, Party Mode shows a slow idle scene and **Choose a song to begin** rather
than implying live audio reactivity.

## Quality and performance

Party Mode supports `auto`, `low`, `medium`, and `high` quality plus `auto`, 30,
or 60 FPS. Auto begins near medium quality, observes measured frame times, and
reduces frame rate or particle work only after sustained pressure. Recovery
requires sustained headroom, preventing rapid quality oscillation. The render
timer stops when the Party window is hidden or closed; artwork decoding,
palette extraction, SVG rendering, and disk access are not per-frame work.

Use the synthetic review tool from the project root:

```powershell
.\tools\dev\run_party_mode_review.ps1 --capture-profile full --scale 1.0
.\tools\dev\run_party_mode_review.ps1 --capture-profile scale-smoke --scale 1.25
.\tools\dev\run_party_mode_review.ps1 --capture-profile scale-smoke --scale 1.5
```

The tool exercises idle, quiet, bass, mid, high, beat, track-change,
missing-artwork, reduced-motion, and all-preset canvas scenes offscreen. Its
full profile also captures the real Party window's visible/hidden overlay,
queue count, and shortcut help, asserting their content and stacking order and
that playback still uses the host's sole `QMediaPlayer`. The two-state
`scale-smoke` profile supplies focused 125% and 150% checks. Both profiles
report sanitized frame metrics. See [Developer tools](../tools/dev/README.md).

## Current limitations

- Audio reactivity depends on decoded-buffer support in the active Qt backend;
  ambient mode is expected on unsupported combinations.
- Party Mode is a desktop presentation, not a mobile mirror, streaming service,
  station scheduler, or editable queue.
- Timing results vary by renderer, display scale, resolution, and GPU. Review
  metrics are diagnostic evidence rather than universal frame-time promises.
- v1.1.0 remains a development line until a separately verified release is
  intentionally tagged and published.
