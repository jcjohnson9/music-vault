"""Keep the small dynamic WinRT closure and its notices reproducible."""
import ast
import hashlib
from pathlib import Path

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]


def test_native_projection_pins_match_runtime_and_release_requirements():
    tree = ast.parse((ROOT / "MusicVault.spec").read_text(encoding="utf-8"))
    assignments = {
        item.targets[0].id: item.value for item in tree.body
        if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)
    }
    pins = ast.literal_eval(assignments["transport_distributions"])
    assert len(pins) == 7
    for filename in ("requirements.txt", "requirements-release.txt"):
        requirements = {
            requirement.name.casefold(): requirement
            for line in (ROOT / filename).read_text().splitlines()
            if line.strip() and not line.startswith("#")
            for requirement in [Requirement(line)]
        }
        for name, version in pins.items():
            requirement = requirements[name.casefold()]
            assert str(requirement.specifier) == f"=={version}"
            if filename == "requirements.txt":
                assert requirement.marker.evaluate({"sys_platform": "win32"})
                assert not requirement.marker.evaluate({"sys_platform": "linux"})
    imports = ast.literal_eval(assignments["transport_imports"])
    for extension in (
        "_winrt", "_winrt_windows_foundation", "_winrt_windows_foundation_collections",
        "_winrt_windows_media", "_winrt_windows_media_interop", "_winrt_windows_storage_streams",
    ):
        assert f"winrt.{extension}" in imports


def test_projection_and_typing_license_texts_are_exact_and_bundled():
    spec = (ROOT / "MusicVault.spec").read_text(encoding="utf-8")
    for filename, expected in (
        ("PYWINRT-3.2.1-MIT.txt", "6e898069e8b3c6d8d23dc70ac7067cc2b7c9db14c36df873026758db82c0891d"),
        ("TYPING-EXTENSIONS-4.16.0-LICENSE.txt", "3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf"),
    ):
        assert hashlib.sha256((ROOT / "licenses" / filename).read_bytes()).hexdigest() == expected
        assert f"('licenses/{filename}', 'licenses')" in spec
