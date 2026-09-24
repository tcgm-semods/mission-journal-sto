"""Build a deterministic keyless external Star Empire ``.semod`` package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid
import zipfile

from installer.mod_manifest import ModManifestError, parse_mod_manifest
from installer.semod_package import (
    SEMOD_FORMAT, SEMOD_SCHEMA, verify_semod_package,
)


_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


class SemodBuildError(RuntimeError):
    """Raised before an incomplete or unsafe mod package is published."""


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _is_link(path: Path) -> bool:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _write_entry(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(name, _ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload)


def build_semod_package(
        *, source_directory: Path, manifest_path: Path, output: Path) -> Path:
    """Publish one exact source namespace and self-verify the result."""
    raw_source = Path(source_directory).expanduser()
    raw_manifest = Path(manifest_path).expanduser()
    if _is_link(raw_source) or _is_link(raw_manifest):
        raise SemodBuildError("source and manifest cannot be links or reparse points")
    source = raw_source.resolve(strict=True)
    manifest_file = raw_manifest.resolve(strict=True)
    destination = Path(output).expanduser().resolve()
    if not source.is_dir() or not manifest_file.is_file():
        raise SemodBuildError("source directory or manifest is missing")
    if destination.suffix.casefold() != ".semod":
        raise SemodBuildError("output must use the .semod extension")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite package: {destination}")
    try:
        manifest_bytes = manifest_file.read_bytes()
        manifest = parse_mod_manifest(manifest_bytes)
    except (OSError, ModManifestError) as error:
        raise SemodBuildError("mod manifest is invalid") from error
    if list(manifest.files) != sorted(manifest.files):
        raise SemodBuildError("mod manifest files must be sorted")

    namespace = manifest.mod_id.replace("-", "_")
    prefix = PurePosixPath("mod", *namespace.split("."))
    required_readme = (prefix / "README.md").as_posix()
    if required_readme not in manifest.files:
        raise SemodBuildError(
            f"mod package must declare {required_readme}")
    payloads: dict[str, bytes] = {"manifest.json": manifest_bytes}
    declared_local: set[str] = set()
    for declared in manifest.files:
        package_path = PurePosixPath(declared)
        try:
            relative = package_path.relative_to(prefix)
        except ValueError as error:
            raise SemodBuildError(
                "first-generation packages must keep every file inside the "
                f"mod namespace {prefix.as_posix()}") from error
        relative_text = relative.as_posix()
        local = source / Path(*relative.parts)
        if _is_link(local) or not local.is_file():
            raise SemodBuildError(f"declared mod source is missing or linked: {relative}")
        resolved = local.resolve(strict=True)
        try:
            resolved.relative_to(source)
        except ValueError as error:
            raise SemodBuildError(f"declared mod source escapes its root: {relative}") from error
        payloads[declared] = resolved.read_bytes()
        declared_local.add(relative_text)

    actual_local: set[str] = set()
    for path in source.rglob("*"):
        if _is_link(path):
            raise SemodBuildError(f"mod source contains a link: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SemodBuildError(f"mod source contains a special file: {path}")
        relative = path.relative_to(source).as_posix()
        if "/__pycache__/" in f"/{relative}/" or relative.endswith((".pyc", ".pyo")):
            continue
        actual_local.add(relative)
    if actual_local != declared_local:
        missing = sorted(declared_local - actual_local)
        extra = sorted(actual_local - declared_local)
        raise SemodBuildError(
            "mod source inventory differs from manifest; "
            f"missing={missing}; extra={extra}")

    records = [{
        "path": name, "size": len(payload), "sha256": _sha(payload),
    } for name, payload in sorted(payloads.items())]
    package_document = {
        "schema": SEMOD_SCHEMA,
        "format": SEMOD_FORMAT,
        "manifest_sha256": _sha(manifest_bytes),
        "payloads": records,
    }
    package_bytes = (json.dumps(
        package_document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.partial.semod")
    try:
        with zipfile.ZipFile(pending, "x") as archive:
            _write_entry(archive, "package.json", package_bytes)
            for name, payload in sorted(payloads.items()):
                _write_entry(archive, name, payload)
        verified = verify_semod_package(pending)
        if (verified.manifest != manifest
                or verified.package_manifest_sha256 != _sha(package_bytes)):
            raise SemodBuildError("package self-verification returned different metadata")
        try:
            os.link(pending, destination)
        except FileExistsError as error:
            raise FileExistsError(
                f"refusing to overwrite concurrently created package: {destination}") from error
        pending.unlink(missing_ok=True)
        return destination
    except Exception:
        pending.unlink(missing_ok=True)
        raise

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_semod_package(
        source_directory=args.source, manifest_path=args.manifest,
        output=args.output)
    print(f"Built and verified {result}")


if __name__ == "__main__":
    main()
