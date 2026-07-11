from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DEFAULT_SIZES = ((1100, 720), (1440, 900), (1920, 1080))
DEFAULT_SCENES = (
    "library",
    "albums",
    "artists",
    "sync_center",
    "settings",
    "empty_playlist",
)
RUNTIME_PREFIX = "MusicVault_Batch4_UI_Runtime_"
OUTPUT_PREFIX = "MusicVault_UI_Review_Output_"


def parse_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("Sizes must use WIDTHxHEIGHT.") from exc
    if not 800 <= width <= 4096 or not 600 <= height <= 2160:
        raise argparse.ArgumentTypeError("Size is outside the supported desktop range.")
    return width, height


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture sanitized Music Vault UI screenshots from synthetic data."
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Screenshot directory. Defaults to a retained TEMP directory.",
    )
    parser.add_argument(
        "--exe",
        type=Path,
        help="Packaged MusicVault.exe to review instead of source run.py.",
    )
    parser.add_argument(
        "--size",
        action="append",
        type=parse_size,
        help="Repeatable WIDTHxHEIGHT capture size.",
    )
    parser.add_argument(
        "--page",
        action="append",
        choices=(*DEFAULT_SCENES, "no_results"),
        help="Repeatable review scene. Defaults to the six-page review matrix.",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="Use Qt offscreen rendering for automated source review.",
    )
    parser.add_argument(
        "--scale",
        type=float,
        choices=(1.0, 1.25, 1.5),
        help="Optional Qt scale factor for a separate high-DPI review process.",
    )
    parser.add_argument("--settle-ms", type=int, default=450)
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_output_directory(project_root: Path, requested: Path | None) -> Path:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix=OUTPUT_PREFIX)).resolve()

    output = requested.expanduser().resolve()
    if is_relative_to(output, project_root):
        permitted = (project_root / ".ui-review").resolve()
        if output != permitted and not is_relative_to(output, permitted):
            raise ValueError(
                "Repository-contained review output is allowed only under .ui-review/."
            )
    output.mkdir(parents=True, exist_ok=True)
    return output


def create_runtime_root(project_root: Path) -> Path:
    runtime = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX)).resolve()
    if is_relative_to(runtime, project_root):
        raise RuntimeError("Synthetic runtime unexpectedly resolved inside the repository.")
    (runtime / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    (runtime / "music_vault").mkdir()
    (runtime / "data").mkdir()
    (runtime / "profile").mkdir()
    return runtime


def copy_public_assets(project_root: Path, runtime: Path) -> None:
    source_icons = project_root / "assets" / "icons"
    target_icons = runtime / "assets" / "icons"
    target_icons.mkdir(parents=True)
    for name in ("music_vault.ico", "music_vault_icon.png"):
        source = source_icons / name
        if source.is_file():
            shutil.copy2(source, target_icons / name)
    shutil.copytree(source_icons / "ui", target_icons / "ui")


def generated_artwork(destination: Path, index: int) -> None:
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen

    colors = (
        ("#1DB954", "#133C55"),
        ("#5B8DEF", "#352D63"),
        ("#F3B84B", "#7B2D3A"),
        ("#25D366", "#1D3557"),
        ("#8B5CF6", "#0F766E"),
        ("#E76F51", "#264653"),
        ("#3A86FF", "#8338EC"),
    )
    first, second = colors[index % len(colors)]
    image = QImage(512, 512, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#0A0F15"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(QPointF(0, 0), QPointF(512, 512))
    gradient.setColorAt(0.0, QColor(first))
    gradient.setColorAt(1.0, QColor(second))
    painter.fillRect(QRectF(0, 0, 512, 512), gradient)
    pen = QPen(QColor(255, 255, 255, 190), 12)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    baseline = 260 + (index % 3) * 12
    for offset, height in enumerate((48, 102, 158, 88, 132, 62, 112)):
        x = 72 + offset * 60
        painter.drawLine(x, baseline - height // 2, x, baseline + height // 2)
    painter.setPen(QPen(QColor(255, 255, 255, 80), 4))
    painter.drawRoundedRect(QRectF(38, 38, 436, 436), 38, 38)
    painter.end()
    if not image.save(str(destination), "PNG"):
        raise RuntimeError("Could not generate synthetic review artwork.")


def seed_synthetic_runtime(project_root: Path, runtime: Path) -> dict[str, int]:
    data = runtime / "data"
    downloads = data / "youtube_downloads"
    covers = data / "synthetic_artwork"
    media = data / "synthetic_sentinels"
    covers.mkdir()
    media.mkdir()

    config = {
        "download_folder": str(downloads),
        "audio_quality": "320",
        "volume_percent": 23,
    }
    (data / "music_vault_config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )

    for index in range(7):
        generated_artwork(covers / f"synthetic_cover_{index + 1}.png", index)

    previous_environment = {
        name: os.environ.get(name)
        for name in ("MUSIC_VAULT_PROJECT_ROOT", "HOME", "USERPROFILE")
    }
    os.environ["MUSIC_VAULT_PROJECT_ROOT"] = str(runtime)
    os.environ["HOME"] = str(runtime / "profile")
    os.environ["USERPROFILE"] = str(runtime / "profile")

    project_text = str(project_root)
    if project_text not in sys.path:
        sys.path.insert(0, project_text)

    try:
        from music_vault.core import paths
        from music_vault.core.db import MusicVaultDB

        paths._resolved_project_root.cache_clear()
        if paths.project_root().resolve() != runtime:
            raise RuntimeError("Music Vault did not resolve the synthetic runtime.")

        database = data / "music_vault.sqlite3"
        db = MusicVaultDB(
            database,
            backup_dir=data / "backups",
            youtube_download_root=downloads,
        )
        artists = (
            "The Local Archive",
            "Neon Cartographers",
            "Cedar & Signal",
            "Static Gardens",
            "Northbound Echo",
            "Glass District",
            "The Quiet Current",
            "Synthetic Ensemble With A Deliberately Long Artist Name",
            None,
        )
        albums = (
            "Midnight Index",
            "Rooms Without Clocks",
            "Green Lines",
            "Signals From Home",
            "City Atlas",
            "Field Notes",
            "A Very Long Synthetic Album Name Designed To Exercise Safe Elision",
            None,
        )
        titles = (
            "Midnight Signal",
            "Paper Constellations",
            "Borrowed Weather",
            "Quiet Circuit",
            "Local Horizon",
            "Windows After Rain",
            "A Deliberately Long Synthetic Track Title That Must Elide Without Breaking Layout",
        )

        track_ids: list[int] = []
        for index in range(24):
            sentinel = media / f"track_{index + 1:02d}.synthetic-audio"
            sentinel.write_bytes(b"synthetic-review-sentinel\n")
            db.upsert_track(
                sentinel,
                title=f"{titles[index % len(titles)]} {index + 1:02d}",
                artist=artists[index % len(artists)],
                album=albums[index % len(albums)],
                duration_seconds=150 + index * 3,
                source_kind="youtube" if index % 5 == 0 else "local",
            )
            row = db.conn.execute(
                "SELECT id FROM tracks WHERE path=?", (str(sentinel.resolve()),)
            ).fetchone()
            track_id = int(row["id"])
            track_ids.append(track_id)
            db.update_track_metadata(
                track_id,
                year=1990 + index if index % 4 else None,
                cover_path=(
                    str((covers / f"synthetic_cover_{index % 7 + 1}.png").resolve())
                    if index % 3 != 0
                    else None
                ),
            )

        playlist_specs = (
            ("Synthetic Focus Mix", track_ids[:10]),
            ("After-Hours Local Review", track_ids[8:18]),
            (
                "A Very Long Synthetic Playlist Name That Must Elide Cleanly In The Sidebar",
                track_ids[::3],
            ),
            ("Empty Playlist", ()),
        )
        for name, members in playlist_specs:
            playlist_id = db.create_playlist(name)
            for track_id in members:
                db.add_track_to_playlist(playlist_id, track_id)

        schema = int(db.conn.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(db.conn.execute("PRAGMA integrity_check").fetchone()[0])
        db.close()
        if schema != 2 or integrity != "ok":
            raise RuntimeError("Synthetic database validation failed.")
    finally:
        try:
            paths._resolved_project_root.cache_clear()  # type: ignore[name-defined]
        except (NameError, AttributeError):
            pass
        for name, value in previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    if (data / "youtube_api_key.txt").exists():
        raise RuntimeError("Synthetic runtime unexpectedly contains an API key.")
    return {"track_count": len(track_ids), "playlist_count": len(playlist_specs)}


def write_review_plan(
    runtime: Path,
    output: Path,
    sizes: tuple[tuple[int, int], ...],
    scenes: tuple[str, ...],
    settle_ms: int,
) -> Path:
    plan = {
        "schema_version": 1,
        "runtime_root": str(runtime),
        "output_dir": str(output),
        "sizes": [{"width": width, "height": height} for width, height in sizes],
        "scenes": list(scenes),
        "settle_ms": settle_ms,
        "expected_capture_count": len(sizes) * len(scenes),
    }
    path = runtime / "ui_review_plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return path


def request_graceful_close(process_id: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    requested = False
    enum_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        nonlocal requested
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process_id and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
            requested = True
        return True

    user32.EnumWindows(enum_type(callback), 0)
    return requested


def wait_for_review(process: subprocess.Popen[str], timeout: int) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if process.poll() is None:
        request_graceful_close(process.pid)
        grace_deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < grace_deadline:
            time.sleep(0.2)
    if process.poll() is None:
        raise TimeoutError(
            "Music Vault did not close gracefully; it was not force-terminated."
        )
    return process.communicate()


def validate_output(
    output: Path,
    runtime: Path,
    expected_count: int,
) -> dict[str, object]:
    manifest_path = output / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("status") != "complete":
        raise RuntimeError("Synthetic UI review manifest did not complete.")
    if payload.get("capture_count") != expected_count:
        raise RuntimeError("Synthetic UI review capture matrix is incomplete.")

    from PySide6.QtGui import QImage

    for capture in payload.get("captures", []):
        filename = capture["file"]
        if Path(filename).name != filename:
            raise RuntimeError("Synthetic screenshot name escaped the output directory.")
        image_path = output / filename
        image = QImage(str(image_path))
        if image.isNull() or image.width() < 800 or image.height() < 600:
            raise RuntimeError(f"Invalid synthetic screenshot: {filename}")
        if capture.get("sha256") != hashlib.sha256(image_path.read_bytes()).hexdigest():
            raise RuntimeError(f"Synthetic screenshot hash mismatch: {filename}")
        colors = set()
        for x_part in range(1, 32):
            for y_part in range(1, 24):
                x = min(image.width() - 1, image.width() * x_part // 32)
                y = min(image.height() - 1, image.height() * y_part // 24)
                colors.add(image.pixelColor(x, y).rgba())
        if len(colors) < 8:
            raise RuntimeError(f"Synthetic screenshot lacks visual content: {filename}")

        requested_width = max(1, int(capture.get("requested_width", image.width())))
        requested_height = max(1, int(capture.get("requested_height", image.height())))
        scale_x = image.width() / requested_width
        scale_y = image.height() / requested_height

        def signal_fraction(left: int, top: int, right: int, bottom: int) -> float:
            x0 = max(0, min(image.width() - 1, int(left * scale_x)))
            y0 = max(0, min(image.height() - 1, int(top * scale_y)))
            x1 = max(x0 + 1, min(image.width(), int(right * scale_x)))
            y1 = max(y0 + 1, min(image.height(), int(bottom * scale_y)))
            sample = [
                image.pixelColor(x, y)
                for x in range(x0, x1, max(1, int(3 * scale_x)))
                for y in range(y0, y1, max(1, int(3 * scale_y)))
            ]
            visible = sum(
                1
                for color in sample
                if color.alpha() >= 128
                and max(color.red(), color.green(), color.blue()) >= 45
            )
            return visible / max(1, len(sample))

        brand_signal = signal_fraction(10, 10, 244, 100)
        player_signal = signal_fraction(
            256,
            requested_height - 160,
            requested_width - 10,
            requested_height - 8,
        )
        if brand_signal < 0.04 or player_signal < 0.025:
            raise RuntimeError(
                f"Synthetic screenshot is missing shared application chrome: {filename}"
            )

    database = runtime / "data" / "music_vault.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
    if schema != 2 or integrity != "ok":
        raise RuntimeError("Synthetic database failed final validation.")
    if (runtime / "data" / "youtube_api_key.txt").exists():
        raise RuntimeError("Synthetic runtime unexpectedly contains an API key.")
    return payload


def safe_delete_runtime(runtime: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    resolved = runtime.resolve()
    if not is_relative_to(resolved, temp_root) or not resolved.name.startswith(RUNTIME_PREFIX):
        raise RuntimeError("Refusing to delete an unverified synthetic runtime path.")
    shutil.rmtree(resolved)


def write_aggregate_manifest(
    output: Path,
    manifests: list[dict[str, object]],
    sizes: tuple[tuple[int, int], ...],
    scenes: tuple[str, ...],
) -> dict[str, object]:
    if not manifests:
        raise RuntimeError("No synthetic UI review captures completed.")

    captures = [
        capture
        for manifest in manifests
        for capture in manifest.get("captures", [])  # type: ignore[union-attr]
    ]
    expected_count = len(sizes) * len(scenes)
    if len(captures) != expected_count:
        raise RuntimeError("Synthetic UI review subprocess matrix is incomplete.")

    pages: list[str] = []
    for manifest in manifests:
        for page in manifest.get("pages", []):  # type: ignore[union-attr]
            if isinstance(page, str) and page not in pages:
                pages.append(page)

    aggregate = dict(manifests[0])
    aggregate.update(
        {
            "status": "complete",
            "finished_at": manifests[-1].get("finished_at"),
            "capture_process_count": len(manifests),
            "requested_capture_count": expected_count,
            "capture_count": len(captures),
            "sizes": [
                {"width": width, "height": height}
                for width, height in sizes
            ],
            "pages": pages,
            "captures": captures,
            "dark_title_bar_applied": all(
                bool(manifest.get("dark_title_bar_applied"))
                for manifest in manifests
            ),
            "runtime_checks": manifests[-1].get("runtime_checks", {}),
        }
    )
    destination = output / "manifest.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return aggregate


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[2]
    output = safe_output_directory(project_root, args.output)
    runtime = create_runtime_root(project_root)
    process: subprocess.Popen[str] | None = None
    completed = False

    try:
        copy_public_assets(project_root, runtime)
        dataset = seed_synthetic_runtime(project_root, runtime)
        sizes = tuple(args.size or DEFAULT_SIZES)
        scenes = tuple(args.page or DEFAULT_SCENES)
        if not 50 <= args.settle_ms <= 5000:
            raise ValueError("--settle-ms must be between 50 and 5000.")
        environment_base = os.environ.copy()
        environment_base.update(
            {
                "MUSIC_VAULT_PROJECT_ROOT": str(runtime),
                "HOME": str(runtime / "profile"),
                "USERPROFILE": str(runtime / "profile"),
            }
        )
        if args.offscreen:
            environment_base["QT_QPA_PLATFORM"] = "offscreen"
        if args.scale is not None:
            environment_base["QT_SCALE_FACTOR"] = str(args.scale)

        if args.exe is not None:
            executable = args.exe.expanduser().resolve()
            if not executable.is_file():
                raise FileNotFoundError(f"Packaged executable not found: {executable}")
            command = [str(executable)]
        else:
            command = [sys.executable, "-B", str(project_root / "run.py")]

        # A fresh process per page/size avoids stale Qt native backing-store
        # regions after rapid stacked-page changes on Windows. It also keeps
        # every screenshot independently reproducible.
        child_manifests: list[dict[str, object]] = []
        for size in sizes:
            for scene in scenes:
                plan = write_review_plan(
                    runtime,
                    output,
                    (size,),
                    (scene,),
                    args.settle_ms,
                )
                environment = environment_base.copy()
                environment["MUSIC_VAULT_UI_REVIEW"] = str(plan)
                process = subprocess.Popen(
                    command,
                    cwd=runtime,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                stdout, stderr = wait_for_review(process, args.timeout)
                if process.returncode != 0:
                    summary = (stderr or stdout).strip()[-1200:]
                    raise RuntimeError(
                        "Music Vault UI review exited with code "
                        f"{process.returncode}: {summary}"
                    )
                process = None
                child_manifests.append(validate_output(output, runtime, 1))

        write_aggregate_manifest(output, child_manifests, sizes, scenes)
        manifest = validate_output(output, runtime, len(sizes) * len(scenes))
        wrong_data = project_root / "dist" / "MusicVault" / "data"
        if wrong_data.exists():
            raise RuntimeError("dist/MusicVault/data was created during UI review.")

        completed = True
        print(
            json.dumps(
                {
                    "status": "complete",
                    "mode": "packaged" if args.exe else "source",
                    "output_dir": str(output),
                    "capture_count": manifest["capture_count"],
                    "sizes": manifest["sizes"],
                    "pages": manifest["pages"],
                    "dark_title_bar_applied": manifest.get("dark_title_bar_applied"),
                    "schema_version": 2,
                    "config_volume_percent": 23,
                    "api_key_present": False,
                    "dataset": dataset,
                },
                indent=2,
            )
        )
        return 0
    finally:
        if process is not None and process.poll() is None:
            print(
                f"Synthetic runtime retained because process {process.pid} is still running: {runtime}",
                file=sys.stderr,
            )
        else:
            safe_delete_runtime(runtime)
        if not completed:
            print(f"Review output retained for diagnosis: {output}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
