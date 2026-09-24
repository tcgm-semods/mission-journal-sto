"""Keyless, game-code-free ``.semod`` verification and extraction."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Any
import uuid
import zipfile

from .mod_manifest import ModManifest, ModManifestError, parse_mod_manifest


SEMOD_FORMAT = "star-empire-mod-package"
SEMOD_SCHEMA = 2
MAX_ENTRIES = 2048
MAX_FILE_SIZE = 64 * 1024 * 1024
MAX_TOTAL_SIZE = 256 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250
FORBIDDEN_NAMES = frozenset({
    "client.py", "render_mixin.py", "hangar_inventory.py",
    "gl_renderer.py", "item_search.py", "protocol.py", "client.exe",
})
FORBIDDEN_SUFFIXES = frozenset({
    ".exe", ".dll", ".pyd", ".pyc", ".pyo", ".patch", ".diff",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".seuimod",
})


class SemodPackageError(ValueError):
    """Raised when a Star Empire mod archive is malformed or unsafe."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _digest(value: Any, label: str) -> str:
    if type(value) is not str:
        raise SemodPackageError(f"{label} must be a SHA-256 string")
    result = value.upper()
    if (len(result) != 64
            or any(char not in "0123456789ABCDEF" for char in result)):
        raise SemodPackageError(f"{label} must be a SHA-256 string")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SemodPackageError(f"duplicate package key: {key}")
        result[key] = value
    return result


def _safe_payload_path(value: Any) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise SemodPackageError("mod package payload path is malformed")
    path = PurePosixPath(value)
    if (path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
            or (value != "manifest.json" and path.parts[:1] != ("mod",))):
        raise SemodPackageError(f"unsafe mod package path: {value}")
    if value != "manifest.json":
        if path.name.casefold() in FORBIDDEN_NAMES:
            raise SemodPackageError(f"game-owned host filename is forbidden: {value}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise SemodPackageError(f"executable/archive payload is forbidden: {value}")
    return path


@dataclass(frozen=True)
class SemodPayload:
    path: PurePosixPath
    size: int
    sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "path": self.path.as_posix(),
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class VerifiedSemodPackage:
    path: Path
    manifest: ModManifest
    package_sha256: str
    package_manifest_sha256: str
    payloads: tuple[SemodPayload, ...]

    def extract(self, destination: Path) -> Path:
        """Extract exact verified payloads to one new directory."""
        target = Path(destination).expanduser().resolve()
        if target.exists():
            raise SemodPackageError(
                f"refusing to overwrite mod install directory: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # tempfile.mkdtemp() deliberately creates an owner-only directory.
        # Renaming that directory into place preserves the restricted Windows
        # ACL, which makes a mod installed by an elevated Manager unreadable to
        # the normally launched game.  A regular mkdir inherits the managed
        # mod root's ACL while the random name keeps extraction collision-safe.
        temporary = target.parent / (
            f".{target.name}.{uuid.uuid4().hex}.partial")
        temporary.mkdir()
        try:
            with zipfile.ZipFile(self.path) as archive:
                for entry in self.payloads:
                    payload = archive.read(entry.path.as_posix())
                    if len(payload) != entry.size or _sha256(payload) != entry.sha256:
                        raise SemodPackageError(
                            f"mod payload changed after verification: {entry.path}")
                    output = temporary / Path(*entry.path.parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(payload)
            temporary.replace(target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target


def _payloads(data: Any) -> tuple[SemodPayload, ...]:
    if type(data) is not list or not data:
        raise SemodPackageError("package payload inventory must be a non-empty list")
    result: list[SemodPayload] = []
    folded: set[str] = set()
    for raw in data:
        if type(raw) is not dict or set(raw) != {"path", "size", "sha256"}:
            raise SemodPackageError("package payload record is malformed")
        path = _safe_payload_path(raw["path"])
        key = path.as_posix().casefold()
        if key in folded:
            raise SemodPackageError("package payload inventory contains a collision")
        folded.add(key)
        size = raw["size"]
        if type(size) is not int or size < 0 or size > MAX_FILE_SIZE:
            raise SemodPackageError(f"package payload size is invalid: {path}")
        result.append(SemodPayload(
            path, size, _digest(raw["sha256"], f"payload hash {path}")))
    if [item.path.as_posix() for item in result] != sorted(
            item.path.as_posix() for item in result):
        raise SemodPackageError("package payload inventory must be sorted")
    return tuple(result)


def verify_semod_package(path: Path) -> VerifiedSemodPackage:
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".semod":
        raise SemodPackageError("select a .semod package")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise SemodPackageError(f"cannot read mod package: {source}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise SemodPackageError("mod package contains too many entries")
        exact: set[str] = set()
        folded: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            if name in exact or name.casefold() in folded:
                raise SemodPackageError(
                    f"duplicate or case-colliding archive path: {name}")
            exact.add(name)
            folded.add(name.casefold())
            raw_path = PurePosixPath(name)
            if (not name or "\\" in name or raw_path.is_absolute()
                    or any(part in {"", ".", ".."} for part in raw_path.parts)):
                raise SemodPackageError(f"unsafe archive path: {name}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise SemodPackageError(f"symbolic links are forbidden: {name}")
            if info.flag_bits & 0x1:
                raise SemodPackageError("encrypted mod packages are forbidden")
            if info.is_dir():
                continue
            if info.file_size > MAX_FILE_SIZE:
                raise SemodPackageError(f"mod package entry is too large: {name}")
            total += info.file_size
            if total > MAX_TOTAL_SIZE:
                raise SemodPackageError("mod package expands beyond the size limit")
            if (info.file_size > 1024 * 1024 and info.compress_size > 0
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
                raise SemodPackageError(f"suspicious compression ratio: {name}")

        if not {"package.json", "manifest.json"}.issubset(exact):
            raise SemodPackageError("mod package is missing required metadata")
        package_bytes = archive.read("package.json")
        try:
            package_data = json.loads(
                package_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SemodPackageError("package.json is not unique-key UTF-8 JSON") from error
        required = {
            "schema", "format", "manifest_sha256", "payloads",
        }
        if (type(package_data) is not dict or set(package_data) != required
                or package_data["schema"] != SEMOD_SCHEMA
                or package_data["format"] != SEMOD_FORMAT):
            raise SemodPackageError("unsupported Star Empire mod package")
        payloads = _payloads(package_data["payloads"])
        expected_names = {
            "package.json",
            *(item.path.as_posix() for item in payloads),
        }
        files = {info.filename for info in infos if not info.is_dir()}
        if files != expected_names:
            raise SemodPackageError("archive inventory differs from declared payloads")
        payload_by_path = {item.path.as_posix(): item for item in payloads}
        manifest_record = payload_by_path.get("manifest.json")
        if manifest_record is None:
            raise SemodPackageError("package payloads omit manifest.json")
        manifest_bytes = archive.read("manifest.json")
        manifest_sha = _sha256(manifest_bytes)
        if (len(manifest_bytes) != manifest_record.size
                or manifest_sha != manifest_record.sha256
                or manifest_sha != _digest(
                    package_data["manifest_sha256"], "manifest_sha256")):
            raise SemodPackageError("mod manifest hash or size does not verify")
        try:
            manifest = parse_mod_manifest(manifest_bytes)
        except ModManifestError as error:
            raise SemodPackageError(str(error)) from error
        if set(manifest.files) != (set(payload_by_path) - {"manifest.json"}):
            raise SemodPackageError(
                "mod manifest files differ from package payloads")
        for item in payloads:
            payload = archive.read(item.path.as_posix())
            if len(payload) != item.size or _sha256(payload) != item.sha256:
                raise SemodPackageError(
                    f"mod payload hash or size does not verify: {item.path}")
            if item.path.as_posix() != "manifest.json" and payload[:2] == b"MZ":
                raise SemodPackageError(
                    f"disguised Windows executable is forbidden: {item.path}")
    return VerifiedSemodPackage(
        source, manifest, _sha256(source.read_bytes()),
        _sha256(package_bytes), payloads)
