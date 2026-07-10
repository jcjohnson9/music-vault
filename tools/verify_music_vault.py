from __future__ import annotations

import ast
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "music_vault"
REQUIRED_FILES = (
    "run.py",
    "music_vault/app.py",
    "music_vault/core/db.py",
    "music_vault/core/importer.py",
    "music_vault/core/youtube_sync.py",
    "music_vault/core/watchtower_status.py",
    "music_vault/core/paths.py",
    "assets/icons/music_vault.ico",
)


def main() -> int:
    missing_files = [
        relative_path
        for relative_path in REQUIRED_FILES
        if not (PROJECT_ROOT / relative_path).is_file()
    ]

    if missing_files:
        print("Missing required files:")
        for relative_path in missing_files:
            print(f"- {relative_path}")
        return 1

    python_files = sorted(PACKAGE_ROOT.rglob("*.py"))
    for path in python_files:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    project_root_text = str(PROJECT_ROOT)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)

    from music_vault.core import paths
    from music_vault.core.watchtower_status import write_watchtower_status
    import music_vault.app as app

    if not callable(write_watchtower_status):
        print("Watchtower status exporter is not callable.")
        return 1

    expected_data_dir = PROJECT_ROOT / "data"
    resolved_paths = {
        "project root": paths.project_root(),
        "data directory": paths.data_dir(),
        "database": paths.database_path(),
        "config": paths.config_path(),
        "status": paths.watchtower_status_path(),
    }

    if resolved_paths["project root"] != PROJECT_ROOT:
        print(f"Unexpected project root: {resolved_paths['project root']}")
        return 1

    if resolved_paths["data directory"] != expected_data_dir:
        print(f"Unexpected data directory: {resolved_paths['data directory']}")
        return 1

    for name in ("database", "config", "status"):
        if resolved_paths[name].parent != expected_data_dir:
            print(f"{name.title()} path is outside the data directory: {resolved_paths[name]}")
            return 1

    print(f"Imported app: {Path(app.__file__).resolve()}")
    print(f"Resolved project root: {resolved_paths['project root']}")
    print(f"Resolved data directory: {resolved_paths['data directory']}")
    print(f"Parsed active Python files: {len(python_files)}")
    print("Music Vault verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
