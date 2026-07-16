from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_TOOL = Path(__file__).with_name("capture_ui_review.py")
OUTPUT_PREFIX = "MusicVault_Batch10_UI_Review_"
OWNER_MARKER = ".music_vault_batch10_review_owner.json"

# Exactly nine distinct states. One process uses 150% scaling, while the
# remaining states cover the three requested desktop sizes without a Cartesian
# product that would create unnecessary captures.
CAPTURE_GROUPS = (
    {
        "size": (1280, 720),
        "scale": 1.0,
        "scenes": (
            "sync_sources_empty",
            "sync_source_add",
            "sync_managed_playlist",
        ),
    },
    {
        "size": (1440, 900),
        "scale": 1.0,
        "scenes": (
            "sync_sources_list",
            "sync_source_edit",
            "sync_complete_issues",
            "sync_source_failures",
        ),
    },
    {
        "size": (1920, 1080),
        "scale": 1.0,
        "scenes": ("sync_all_running",),
    },
    {
        "size": (1280, 720),
        "scale": 1.5,
        "scenes": ("sync_source_remove",),
    },
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the bounded, offline Batch 10 Sync Center review and delete "
            "sanitized captures after validation unless --keep-captures is used."
        )
    )
    parser.add_argument("--exe", type=Path, help="Review the packaged MusicVault.exe.")
    parser.add_argument("--output", type=Path, help="Caller-selected capture directory.")
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--keep-captures", action="store_true")
    parser.add_argument("--settle-ms", type=int, default=450)
    parser.add_argument("--timeout", type=int, default=240)
    return parser.parse_args(argv)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _output_directory(requested: Path | None) -> tuple[Path, str]:
    if requested is None:
        output = Path(tempfile.mkdtemp(prefix=OUTPUT_PREFIX)).resolve()
    else:
        output = requested.expanduser().resolve()
        permitted = (PROJECT_ROOT / ".ui-review").resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        if not _is_relative_to(output, permitted) and not _is_relative_to(output, temp):
            raise ValueError("Batch 10 review output is allowed only in TEMP or .ui-review/.")
        if _is_relative_to(output, temp) and not output.name.startswith(OUTPUT_PREFIX):
            raise ValueError(
                f"TEMP output directories must begin with {OUTPUT_PREFIX}."
            )
        if output.exists() and any(output.iterdir()):
            raise ValueError("Refusing to use a non-empty Batch 10 review directory.")
        output.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    marker = output / OWNER_MARKER
    marker.write_text(
        json.dumps({"schema_version": 1, "token": token, "root": str(output)}) + "\n",
        encoding="utf-8",
    )
    return output, token


def _verify_owned_output(output: Path, token: str) -> Path:
    resolved = output.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    review = (PROJECT_ROOT / ".ui-review").resolve()
    location_ok = (
        _is_relative_to(resolved, temp) and resolved.name.startswith(OUTPUT_PREFIX)
    ) or _is_relative_to(resolved, review)
    if not location_ok or resolved.is_symlink():
        raise RuntimeError("Refusing to clean an unverified Batch 10 review root.")
    try:
        marker = json.loads((resolved / OWNER_MARKER).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Batch 10 review ownership marker is unavailable.") from exc
    if marker.get("token") != token or Path(str(marker.get("root"))).resolve() != resolved:
        raise RuntimeError("Batch 10 review ownership marker does not match this run.")
    return resolved


def _safe_delete_group(output: Path, group_index: int, token: str) -> None:
    root = _verify_owned_output(output, token)
    group = (root / f"group_{group_index:02d}").resolve()
    if group.parent != root or group.name != f"group_{group_index:02d}" or group.is_symlink():
        raise RuntimeError("Refusing to clean an unverified Batch 10 review group.")
    if group.exists():
        shutil.rmtree(group)


def _safe_delete_output(output: Path, token: str) -> None:
    shutil.rmtree(_verify_owned_output(output, token))


def _run_group(
    group_index: int,
    group: dict[str, object],
    *,
    output: Path,
    args: argparse.Namespace,
) -> dict[str, object]:
    width, height = group["size"]  # type: ignore[misc]
    scale = float(group["scale"])
    scenes = tuple(group["scenes"])  # type: ignore[arg-type]
    child_output = output / f"group_{group_index:02d}"
    command = [
        sys.executable,
        "-B",
        str(CAPTURE_TOOL),
        "--output",
        str(child_output),
        "--size",
        f"{width}x{height}",
        "--settle-ms",
        str(args.settle_ms),
        "--timeout",
        str(args.timeout),
    ]
    for scene in scenes:
        command.extend(("--page", str(scene)))
    if args.offscreen:
        command.append("--offscreen")
    if scale != 1.0:
        command.extend(("--scale", str(scale)))
    if args.exe is not None:
        command.extend(("--exe", str(args.exe.expanduser().resolve())))

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(args.timeout * len(scenes) + 60, 300),
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise RuntimeError(
            f"Batch 10 review group {group_index} failed with code "
            f"{completed.returncode}: {detail}"
        )
    manifest = json.loads((child_output / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "complete" or manifest.get("capture_count") != len(scenes):
        raise RuntimeError(f"Batch 10 review group {group_index} is incomplete.")
    for capture in manifest.get("captures", []):
        capture["scale_factor"] = scale
    return manifest


def _aggregate(
    output: Path,
    manifests: list[dict[str, object]],
    *,
    owner_token: str,
) -> dict[str, object]:
    captures: list[dict[str, object]] = []
    behavior_sets: list[dict[str, object]] = []
    for group_index, manifest in enumerate(manifests, start=1):
        child = output / f"group_{group_index:02d}"
        behaviors = (manifest.get("runtime_checks") or {}).get(  # type: ignore[union-attr]
            "multi_source_behaviors", {}
        )
        if isinstance(behaviors, dict):
            behavior_sets.append(behaviors)
        for capture in manifest.get("captures", []):  # type: ignore[union-attr]
            if not isinstance(capture, dict):
                continue
            source = child / str(capture["file"])
            destination = output / str(capture["file"])
            if destination.exists():
                raise RuntimeError("The Batch 10 review matrix produced a duplicate filename.")
            shutil.copy2(source, destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != capture.get("sha256"):
                raise RuntimeError("A Batch 10 capture changed while aggregating.")
            captures.append(dict(capture))

    expected_scenes = {
        str(scene)
        for group in CAPTURE_GROUPS
        for scene in group["scenes"]  # type: ignore[union-attr]
    }
    actual_scenes = {str(capture.get("scene")) for capture in captures}
    scaled = [capture for capture in captures if capture.get("scale_factor") == 1.5]
    if len(captures) != 9 or actual_scenes != expected_scenes or len(scaled) != 1:
        raise RuntimeError("The Batch 10 review did not produce its exact nine-state matrix.")
    if not behavior_sets or any(
        behaviors.get("scenario_completed") is not True for behaviors in behavior_sets
    ):
        raise RuntimeError("The Batch 10 offline behavior smoke was not completed.")

    aggregate = {
        "schema_version": 1,
        "application": "Music Vault",
        "review_kind": "batch10_multiple_sources",
        "status": "complete",
        "capture_count": len(captures),
        "capture_process_count": len(manifests),
        "scenes": sorted(actual_scenes),
        "sizes": sorted(
            {
                (
                    int(capture["requested_width"]),
                    int(capture["requested_height"]),
                )
                for capture in captures
            }
        ),
        "scale_factors": sorted({float(capture["scale_factor"]) for capture in captures}),
        "captures": captures,
        "multi_source_behaviors": behavior_sets[-1],
        "synthetic_only": True,
        "network_attempt_count": 0,
        "api_key_present": False,
    }
    (output / "manifest.json").write_text(
        json.dumps(aggregate, indent=2) + "\n", encoding="utf-8"
    )
    for group_index in range(1, len(manifests) + 1):
        _safe_delete_group(output, group_index, owner_token)
    return aggregate


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 50 <= args.settle_ms <= 5000:
        raise ValueError("--settle-ms must be between 50 and 5000.")
    output, owner_token = _output_directory(args.output)
    completed = False
    try:
        manifests = [
            _run_group(index, group, output=output, args=args)
            for index, group in enumerate(CAPTURE_GROUPS, start=1)
        ]
        aggregate = _aggregate(output, manifests, owner_token=owner_token)
        summary = {
            "status": "complete",
            "mode": "packaged" if args.exe else "source",
            "capture_count": aggregate["capture_count"],
            "scenes": aggregate["scenes"],
            "sizes": aggregate["sizes"],
            "scale_factors": aggregate["scale_factors"],
            "synthetic_multi_source_smoke": "passed",
            "captures_retained": bool(args.keep_captures),
            "output_dir": str(output) if args.keep_captures else None,
        }
        print(json.dumps(summary, indent=2))
        completed = True
        return 0
    finally:
        if completed and not args.keep_captures:
            _safe_delete_output(output, owner_token)
        elif not completed:
            print(f"Batch 10 review output retained for diagnosis: {output}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
