"""Build this mod's .semod package for CI (invoked by .github/workflows/release.yml)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from installer.mod_manifest import parse_mod_manifest  # noqa: E402
from tools.build_semod_package import build_semod_package  # noqa: E402


def main() -> None:
    manifest_path = ROOT / "manifest.json"
    manifest = parse_mod_manifest(manifest_path.read_bytes())
    output = ROOT / f"{manifest.mod_id}.semod"
    if output.exists():
        output.unlink()
    result = build_semod_package(
        source_directory=ROOT / "source",
        manifest_path=manifest_path,
        output=output)
    print(f"Built {result}")


if __name__ == "__main__":
    main()
