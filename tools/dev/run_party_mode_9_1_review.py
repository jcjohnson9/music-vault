"""Deterministic Batch 9.1 Party Mode visual and performance review.

The tool uses temporary synthetic artwork, metadata, media paths, lyrics, and
provider results.  Python network access is blocked for the entire process.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import wave
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Scene:
    name: str
    preset: str
    width: int
    height: int
    stage: float = 0.0
    reduced: bool = False


MOTION_SCENES = (
    Scene("01_static", "static", 1280, 720),
    Scene("02_starfield_fixed_album", "starfield", 1920, 1080),
    Scene("03_aurora_low", "aurora", 1920, 1080, 0.15),
    Scene("04_aurora_high", "aurora", 2560, 1440, 0.90),
    Scene("05_orb_compressed", "orb_cluster", 1280, 720, 0.00),
    Scene("06_orb_mid_expansion", "orb_cluster", 1920, 1080, 0.25),
    Scene("07_orb_full_expansion", "orb_cluster", 2560, 1440, 0.50),
    Scene("08_orb_mid_contraction", "orb_cluster", 1920, 1080, 0.75),
    Scene("09_orb_accent_subset", "orb_cluster", 1920, 1080, 0.35),
    Scene("10_firework_initial", "fireworks", 1280, 720, 0.02),
    Scene("11_firework_expanded", "fireworks", 1920, 1080, 0.22),
    Scene("12_firework_falling", "fireworks", 2560, 1440, 0.72),
    Scene("13_firework_fading", "fireworks", 1920, 1080, 1.38),
    Scene("14_pulse_minimum", "pulse", 1280, 720, 0.00),
    Scene("15_pulse_maximum", "pulse", 1920, 1080, 0.50),
    Scene("22_reduced_orb_cluster", "orb_cluster", 2560, 1440, 0.50, True),
)

LYRIC_SCENES = (
    ("16_synced_previous_current_next", "synced", 1920, 1080, True),
    ("17_synced_controls_hidden", "synced", 1280, 720, False),
    ("18_unsynced_lyrics", "plain", 1920, 1080, True),
    ("19_no_lyrics", "none", 1280, 720, True),
    ("20_instrumental", "instrumental", 1920, 1080, True),
    ("21_long_lyric_line", "long", 2560, 1440, True),
)


def _network_guard() -> None:
    guarded = {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.sendto",
    }

    def audit(event: str, _arguments: tuple[object, ...]) -> None:
        if event in guarded:
            raise RuntimeError(f"Batch 9.1 review blocked network event: {event}")

    sys.addaudithook(audit)


def _percentile(values: list[float], fraction: float = 0.95) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))]


def _image_metrics(image: Any) -> dict[str, object]:
    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        raise RuntimeError("empty visual capture")
    colors: set[int] = set()
    luminance: list[float] = []
    for row in range(30):
        y = min(height - 1, round((row + 0.5) * height / 30))
        for column in range(50):
            x = min(width - 1, round((column + 0.5) * width / 50))
            color = image.pixelColor(x, y)
            colors.add(int(color.rgba()))
            luminance.append(
                (0.2126 * color.red() + 0.7152 * color.green() + 0.0722 * color.blue())
                / 255.0
            )
    if len(colors) < 8:
        raise RuntimeError("capture appears blank")
    bright = sum(value > 0.94 for value in luminance) / len(luminance)
    if bright > 0.55:
        raise RuntimeError("capture exceeded the brightness guardrail")
    return {
        "width": width,
        "height": height,
        "sampled_colors": len(colors),
        "mean_luminance": round(statistics.fmean(luminance), 4),
        "bright_fraction": round(bright, 4),
    }


def _make_artwork(QImage: Any, QColor: Any, QPainter: Any, QLinearGradient: Any) -> Any:
    image = QImage(512, 512, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#08121f"))
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 512, 512)
    gradient.setColorAt(0.0, QColor("#18a9a0"))
    gradient.setColorAt(0.55, QColor("#516bd8"))
    gradient.setColorAt(1.0, QColor("#9d4fc2"))
    painter.fillRect(image.rect(), gradient)
    painter.setPen(QColor(255, 255, 255, 95))
    painter.setBrush(QColor(5, 10, 20, 125))
    painter.drawEllipse(70, 70, 372, 372)
    painter.setBrush(QColor(255, 255, 255, 155))
    painter.drawEllipse(210, 210, 92, 92)
    painter.end()
    return image


def _capture(widget: Any, app: Any, path: Path) -> dict[str, object]:
    widget.show()
    widget.update()
    app.processEvents()
    pixmap = widget.grab()
    if pixmap.isNull() or not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"could not capture {path.name}")
    metrics = _image_metrics(pixmap.toImage())
    metrics["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return metrics


def _write_synthetic_wav(path: Path) -> None:
    """Write a valid, bounded silence fixture without reading personal media."""

    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8_000)
        stream.writeframes(b"\x00\x00" * 8_000)


def _stage_motion(canvas: Any, scene: Scene, api: dict[str, Any]) -> Any:
    engine = api["PartyVisualEngine"](
        seed=9101,
        preset=scene.preset,
        quality="high",
        reduced_motion=scene.reduced,
    )
    features = {
        "energy": scene.stage if scene.preset == "aurora" else 0.55,
        "bass": scene.stage if scene.preset == "aurora" else 0.58,
        "low_mid": scene.stage * 0.82 if scene.preset == "aurora" else 0.48,
        "mid": scene.stage * 0.70 if scene.preset == "aurora" else 0.40,
        "high": scene.stage * 0.58 if scene.preset == "aurora" else 0.34,
        "audio_reactivity_available": True,
    }
    frame = engine.update(0.0, features)
    if scene.preset == "aurora":
        for _ in range(24):
            frame = engine.update(0.05, features)
    elif scene.preset == "orb_cluster":
        motion = replace(
            frame.motion,
            phrase_phase=scene.stage,
            accent_trigger=scene.name.startswith("09_"),
        )
        orbs, radius, rotation = engine._orb_simulation.update(
            0.0,
            motion,
            count=engine.particle_budget("orb_cluster"),
            energy=0.48,
            reduced_motion=scene.reduced,
            force_accent=scene.name.startswith("09_"),
        )
        frame = replace(
            frame,
            motion=motion,
            orbs=orbs,
            cluster_radius_scale=radius,
            orb_rotation=rotation,
        )
    elif scene.preset == "fireworks":
        engine._fireworks.spawn(
            particles_per_burst=52,
            maximum_bursts=3,
            maximum_particles=156,
            reduced_motion=False,
            center=(0.18, 0.30),
        )
        elapsed = 0.0
        particles = ()
        while elapsed < scene.stage:
            delta = min(0.05, scene.stage - elapsed)
            particles = engine._fireworks.update(
                delta,
                trigger=False,
                particles_per_burst=52,
                maximum_bursts=3,
                maximum_particles=156,
                reduced_motion=False,
            )
            elapsed += delta
        frame = replace(
            frame,
            firework_particles=particles,
            active_firework_bursts=engine._fireworks.active_burst_count,
        )
    elif scene.preset == "pulse":
        motion = replace(frame.motion, bar_phase=scene.stage)
        frame = replace(
            frame,
            motion=motion,
            album_transform=api["album_transform_for_preset"](
                "pulse", scene.stage, reduced_motion=scene.reduced
            ),
        )
    canvas._engine = engine
    canvas._frame = frame
    return frame


def _motion_captures(api: dict[str, Any], app: Any, captures: Path, artwork: Any) -> list[dict[str, object]]:
    results = []
    for scene in MOTION_SCENES:
        canvas = api["PartyCanvas"](
            seed=9101,
            preset=scene.preset,
            quality="high",
        )
        canvas.set_reduced_motion(scene.reduced)
        canvas.resize(scene.width, scene.height)
        canvas.set_artwork(api["QPixmap"].fromImage(artwork))
        canvas.set_palette(api["PaletteExtractor"]().extract(artwork))
        canvas.set_track_text(
            "Synthetic Signal",
            "Music Vault Review",
            "Synthetic Collection",
        )
        canvas.set_playback_state(True, True)
        frame = _stage_motion(canvas, scene, api)
        if scene.preset != "pulse":
            transform = frame.album_transform
            if (transform.scale, transform.translate_x, transform.translate_y, transform.rotation_degrees) != (1.0, 0.0, 0.0, 0.0):
                raise RuntimeError(f"album motion invariant failed in {scene.name}")
        metrics = _capture(canvas, app, captures / f"{scene.name}.png")
        results.append(
            {
                "scene": scene.name,
                "preset": scene.preset,
                "reduced_motion": scene.reduced,
                "album_scale": frame.album_transform.scale,
                "orb_count": len(frame.orbs),
                "firework_particle_count": len(frame.firework_particles),
                **metrics,
            }
        )
        canvas.stop_rendering()
        canvas.close()
        canvas.deleteLater()
        app.processEvents()
    return results


def _lyrics_captures(api: dict[str, Any], app: Any, captures: Path, media: Path) -> list[dict[str, object]]:
    QObject, Signal, Qt = api["QObject"], api["Signal"], api["Qt"]
    QMediaPlayer, QSlider, QMainWindow = api["QMediaPlayer"], api["QSlider"], api["QMainWindow"]

    class Player(QObject):
        positionChanged = Signal(int)
        durationChanged = Signal(int)
        playbackStateChanged = Signal(object)

        def position(self): return 2_500
        def duration(self): return 180_000
        def playbackState(self): return QMediaPlayer.PlaybackState.PausedState
        def isSeekable(self): return True
        def setPosition(self, _value): pass

    class Output:
        def isMuted(self): return False
        def setMuted(self, _muted): pass

    class DB:
        def get_track(self, track_id):
            if track_id != 1:
                return None
            return {
                "path": str(media),
                "title": "Synthetic Signal",
                "artist": "Music Vault Review",
                "album": "Synthetic Collection",
                "cover_path": "",
            }

    class Host(QMainWindow):
        def __init__(self):
            super().__init__()
            self.config = {
                **api["PARTY_MODE_DEFAULTS"],
                **api["LYRICS_DEFAULTS"],
                "party_mode_reduced_motion": True,
                "party_mode_auto_hide_overlay": False,
            }
            self.db, self.current_track_id = DB(), 1
            self.player, self.audio_output = Player(), Output()
            self.volume_percent = 65
            self.volume_slider = QSlider(Qt.Orientation.Horizontal)
            self.volume_slider.setRange(0, 100)
            self.volume_slider.setValue(65)
            self.autoplay_enabled, self.shuffle_enabled = True, False
            self.repeat_mode, self.manual_queue = "off", []
            self.save_count = 0

        def save_config(self): self.save_count += 1
        def toggle_play(self): pass
        def play_previous(self): pass
        def play_next(self): pass
        def toggle_autoplay(self): self.autoplay_enabled = not self.autoplay_enabled
        def toggle_shuffle(self): self.shuffle_enabled = not self.shuffle_enabled
        def cycle_repeat(self): self.repeat_mode = "all"

    host = Host()
    window = api["PartyModeWindow"](host)
    window._lyrics_settings["party_mode_lyrics_enabled"] = True
    identity = api["TrackLyricsIdentity"](
        1,
        "Synthetic Signal",
        "Music Vault Review",
        "Synthetic Collection",
        180_000,
        media,
    )
    synced = api["LyricsResult"](
        api["LyricsStatus"].AVAILABLE,
        identity,
        api["LyricsSource"].MANUAL,
        (
            api["LyricLine"](1000, "Previous synthetic lyric"),
            api["LyricLine"](2000, "Current synthetic lyric"),
            api["LyricLine"](3000, "Next synthetic lyric"),
        ),
    )
    plain = api["LyricsResult"](
        api["LyricsStatus"].AVAILABLE,
        identity,
        api["LyricsSource"].MANUAL,
        (),
        "Synthetic unsynchronized line one\n\nSynthetic line two\nSynthetic line three " * 8,
    )
    results = []
    try:
        window.canvas.set_track_text(
            "Synthetic Signal", "Music Vault Review", "Synthetic Collection"
        )
        for name, mode, width, height, controls in LYRIC_SCENES:
            window.resize(width, height)
            window.show()
            if mode == "synced":
                window.lyrics_panel.show_result(synced)
                window.lyrics_panel.set_position(2_500, force=True)
            elif mode == "plain":
                window.lyrics_panel.show_result(plain)
            elif mode == "none":
                window.lyrics_panel.show_state("No lyrics available")
            elif mode == "instrumental":
                window.lyrics_panel.show_result(
                    api["LyricsResult"](api["LyricsStatus"].INSTRUMENTAL, identity)
                )
            else:
                long_result = replace(
                    synced,
                    synced_lines=(
                        api["LyricLine"](
                            1000,
                            "A deliberately long synthetic lyric line that wraps cleanly while preserving the approved album, title, artist, and playback-bar geometry across a high-DPI review surface.",
                        ),
                    ),
                )
                window.lyrics_panel.show_result(long_result)
                window.lyrics_panel.set_position(2_500, force=True)
            window._position_lyrics_panel()
            if controls:
                window.show_overlay()
            else:
                window.hide_overlay()
            app.processEvents()
            if not window.lyrics_panel.isVisible():
                raise RuntimeError(f"lyrics panel hidden in {name}")
            panel_bottom = window.lyrics_panel.geometry().bottom()
            controls_top = window.controls_panel.mapTo(
                window.centralWidget(), window.controls_panel.rect().topLeft()
            ).y()
            if panel_bottom >= controls_top:
                raise RuntimeError(f"lyrics/control overlap in {name}")
            metrics = _capture(window, app, captures / f"{name}.png")
            results.append(
                {
                    "scene": name,
                    "mode": mode,
                    "controls_visible": controls,
                    "panel_above_controls": True,
                    **metrics,
                }
            )
        # Exercise non-captured states and persistence without provider access.
        window.lyrics_panel.show_state("Finding lyrics…")
        window._lyrics_settings["lyrics_lookup_consent_version"] = 1
        window.toggle_lyrics()
        window.toggle_lyrics()
        if host.save_count != 2:
            raise RuntimeError("lyrics persistence action was not recorded")
    finally:
        window.shutdown()
        host.close()
        window.deleteLater()
        host.deleteLater()
        app.processEvents()
    return results


def _benchmarks(api: dict[str, Any], app: Any) -> dict[str, object]:
    QImage, Qt = api["QImage"], api["Qt"]
    target = QImage(1920, 1080, QImage.Format.Format_ARGB32)
    target.fill(Qt.GlobalColor.transparent)
    results: dict[str, object] = {}
    for name, preset in (("orb_cluster", "orb_cluster"), ("fireworks", "fireworks"), ("aurora", "aurora")):
        canvas = api["PartyCanvas"](seed=77, preset=preset, quality="high")
        canvas.resize(1920, 1080)
        canvas.set_track_text("Synthetic Signal", "Music Vault Review")
        peak_firework_particles = 0
        if preset == "fireworks":
            for center in ((0.18, 0.30), (0.80, 0.27), (0.22, 0.68)):
                canvas._engine._fireworks.spawn(
                    particles_per_burst=52,
                    maximum_bursts=3,
                    maximum_particles=156,
                    reduced_motion=False,
                    center=center,
                )
                peak_firework_particles = max(
                    peak_firework_particles,
                    canvas._engine._fireworks.live_particle_count,
                )
        simulation, paint = [], []
        frame = None
        for _ in range(90):
            started = time.perf_counter_ns()
            frame = canvas._engine.update(1 / 60, {"energy": 0.62, "bass": 0.68})
            simulation.append((time.perf_counter_ns() - started) / 1_000_000)
            canvas._frame = frame
            started = time.perf_counter_ns()
            canvas.render(target)
            paint.append((time.perf_counter_ns() - started) / 1_000_000)
        results[name] = {
            "mean_simulation_ms": round(statistics.fmean(simulation), 3),
            "p95_simulation_ms": round(_percentile(simulation), 3),
            "mean_paint_ms": round(statistics.fmean(paint), 3),
            "p95_paint_ms": round(_percentile(paint), 3),
            "orbs": len(frame.orbs),
            "firework_particles": len(frame.firework_particles),
            "peak_firework_particles": peak_firework_particles,
        }
        canvas.deleteLater()
    static = api["PartyCanvas"](preset="static")
    static.start_rendering()
    results["static"] = {"visual_timer_active": static.rendering_active}
    if static.rendering_active:
        raise RuntimeError("Static retained its high-frequency timer")

    lines = tuple(api["LyricLine"](index * 1000, f"Synthetic {index}") for index in range(400))
    timeline = api["LyricsTimeline"](lines)
    started = time.perf_counter_ns()
    for index in range(10_000):
        timeline.index_at(index * 50)
    timeline_ms = (time.perf_counter_ns() - started) / 1_000_000
    results["lyrics_timeline"] = {
        "lookups": 10_000,
        "total_ms": round(timeline_ms, 3),
        "mean_ms": round(timeline_ms / 10_000, 6),
    }

    identity = api["TrackLyricsIdentity"](
        901,
        "Synthetic Signal",
        "Music Vault Review",
        "Synthetic Collection",
        180_000,
        api["runtime"] / "synthetic.wav",
    )
    cached = api["LyricsResult"](
        api["LyricsStatus"].AVAILABLE,
        identity,
        api["LyricsSource"].PROVIDER,
        tuple(
            api["LyricLine"](index * 1_000, f"Synthetic {index}")
            for index in range(80)
        ),
    )
    cache = api["LyricsCache"](api["runtime"] / "data" / "lyrics")
    cache.store(cached)
    started = time.perf_counter_ns()
    for _ in range(500):
        result = cache.lookup(identity)
        if result is None or not result.synchronized:
            raise RuntimeError("synthetic lyrics cache lookup failed")
    cache_ms = (time.perf_counter_ns() - started) / 1_000_000
    results["lyrics_cache"] = {
        "lookups": 500,
        "total_ms": round(cache_ms, 3),
        "mean_ms": round(cache_ms / 500, 6),
    }

    aurora = api["PartyVisualEngine"](seed=902, preset="aurora", quality="high")
    for _ in range(12):
        aurora.update(
            1 / 60,
            {
                "energy": 1.0,
                "bass": 1.0,
                "low_mid": 1.0,
                "mid": 1.0,
                "high": 1.0,
            },
        )
    release = [
        aurora.update(
            1 / 60,
            {
                "energy": 0.0,
                "bass": 0.0,
                "low_mid": 0.0,
                "mid": 0.0,
                "high": 0.0,
            },
        ).bass
        for _ in range(120)
    ]
    if not all(
        next_value <= value for value, next_value in zip(release, release[1:])
    ):
        raise RuntimeError("Aurora release smoothing was not monotonic")
    results["aurora_release"] = {
        "initial_bass": round(release[0], 4),
        "one_second_bass": round(release[59], 4),
        "two_second_bass": round(release[-1], 4),
        "monotonic": True,
    }
    app.processEvents()
    return results


def run(output: Path | None, scale: float) -> dict[str, object]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = str(scale)
    if os.name == "nt" and not os.environ.get("QT_QPA_FONTDIR"):
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if fonts.is_dir():
            os.environ["QT_QPA_FONTDIR"] = str(fonts)
    os.environ["MUSIC_VAULT_DISABLE_NETWORK"] = "1"
    _network_guard()
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    with tempfile.TemporaryDirectory(prefix="MusicVault_Batch9_1_Review_") as temp:
        runtime = Path(temp)
        os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(runtime)
        (runtime / "data").mkdir()
        (runtime / "music-vault.portable.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "product": "Music Vault",
                    "portable": True,
                    "data_directory": "data",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        from PySide6.QtCore import QObject, Qt, Signal
        from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtWidgets import QApplication, QMainWindow, QSlider
        from music_vault.lyrics.models import (
            LyricLine,
            LyricsResult,
            LyricsSource,
            LyricsStatus,
            TrackLyricsIdentity,
        )
        from music_vault.lyrics.cache import LyricsCache
        from music_vault.ui.party_lyrics import LYRICS_DEFAULTS, LyricsTimeline
        from music_vault.ui.party_mode import PARTY_MODE_DEFAULTS, PartyModeWindow
        from music_vault.ui.party_palette import PaletteExtractor
        from music_vault.ui.party_visuals import (
            PartyCanvas,
            PartyVisualEngine,
            album_transform_for_preset,
        )

        app = QApplication.instance() or QApplication([])
        api = locals()
        artwork = _make_artwork(QImage, QColor, QPainter, QLinearGradient)
        media = runtime / "synthetic.wav"
        _write_synthetic_wav(media)
        captures = runtime / "captures"
        captures.mkdir()
        visual = _motion_captures(api, app, captures, artwork)
        lyrics = _lyrics_captures(api, app, captures, media)
        all_captures = sorted([*visual, *lyrics], key=lambda item: item["scene"])
        if len(all_captures) != 22:
            raise RuntimeError(f"expected 22 review captures, got {len(all_captures)}")
        benchmarks = _benchmarks(api, app)
        payload = {
            "schema_version": 1,
            "review": "Music Vault Batch 9.1 Party Mode",
            "synthetic_only": True,
            "network_used": False,
            "personal_data_used": False,
            "qt_scale_factor": scale,
            "capture_count": len(all_captures),
            "captures": all_captures,
            "benchmarks": benchmarks,
            "temporary_runtime_removed": True,
        }
        if output is not None:
            destination = output.resolve()
            permitted = (PROJECT_ROOT / ".ui-review").resolve()
            if destination.is_relative_to(PROJECT_ROOT) and not destination.is_relative_to(permitted):
                raise ValueError("repository output is allowed only below .ui-review")
            destination.mkdir(parents=True, exist_ok=True)
            for source in captures.glob("*.png"):
                (destination / source.name).write_bytes(source.read_bytes())
            (destination / "batch9-1-review.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scale", type=float, choices=(1.0, 1.25, 1.5), default=1.0)
    args = parser.parse_args(argv)
    try:
        payload = run(args.output, args.scale)
    except Exception as exc:
        print(f"Batch 9.1 review failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"Batch 9.1 synthetic review passed: {payload['capture_count']} states; "
        f"scale {payload['qt_scale_factor']}."
    )
    print(json.dumps(payload["benchmarks"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
