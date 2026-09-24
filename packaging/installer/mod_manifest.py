"""Strict, source-only manifest contract for general Star Empire mods."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping


MOD_FORMAT = "star-empire-mod"
MOD_SCHEMA = 1
_MOD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:\.(0|[1-9][0-9]*))?"
    r"(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
_ENTRYPOINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PERMISSION = re.compile(r"^[a-z][a-z0-9.-]{1,47}$")
_SHA256 = re.compile(r"^[0-9A-F]{64}$")
_ALLOWED_FILE_SUFFIXES = frozenset({
    ".py", ".png", ".json", ".toml", ".txt", ".md",
})


class ModManifestError(ValueError):
    """Raised when a mod manifest is ambiguous or unsafe."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModManifestError(f"duplicate mod manifest key: {key}")
        result[key] = value
    return result


def _require_string(value: Any, label: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise ModManifestError(f"{label} must be a string")
    result = value.strip()
    if not result or len(result) > maximum:
        raise ModManifestError(f"{label} is empty or too long")
    return result


def _mod_id(value: Any, label: str = "mod_id") -> str:
    result = _require_string(value, label, maximum=64)
    if not _MOD_ID.fullmatch(result):
        raise ModManifestError(
            f"{label} must use lowercase letters, numbers, dots, dashes or underscores")
    return result


def _version(value: Any, label: str) -> str:
    result = _require_string(value, label, maximum=64)
    if not _VERSION.fullmatch(result):
        raise ModManifestError(
            f"{label} must use version form X.Y or X.Y.Z")
    return result


def version_core(value: str) -> tuple[int, int, int]:
    """Return the numeric comparison core of a validated mod version."""
    validated = _version(value, "version")
    parts = validated.split("-", 1)[0].split(".")
    if len(parts) == 2:
        parts.append("0")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def version_at_least(actual: str, required: str) -> bool:
    return version_core(actual) >= version_core(required)


@dataclass(frozen=True)
class ModDependency:
    mod_id: str
    version_min: str

    def to_mapping(self) -> dict[str, str]:
        return {"mod_id": self.mod_id, "version_min": self.version_min}


@dataclass(frozen=True)
class GithubUpdateSource:
    repository: str
    asset: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "type": "github-release",
            "repository": self.repository,
            "asset": self.asset,
        }


@dataclass(frozen=True)
class CompatibleGameBuild:
    game_version: str
    client_sha256: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "game_version": self.game_version,
            "client_sha256": self.client_sha256,
        }


@dataclass(frozen=True)
class ModCompatibility:
    """Game versions or exact builds supported by one external mod."""

    game_builds: tuple[CompatibleGameBuild, ...] = ()
    game_versions: tuple[str, ...] = ()

    def supports(self, game_version: str, client_sha256: str = "") -> bool:
        version = str(game_version).strip()
        if self.game_versions:
            return version in self.game_versions
        identity = (str(game_version).strip(), str(client_sha256).upper())
        return any(
            identity == (build.game_version, build.client_sha256)
            for build in self.game_builds)

    def to_mapping(self) -> dict[str, Any]:
        if self.game_versions:
            return {
                "mode": "game-version",
                "game_versions": list(self.game_versions),
            }
        return {
            "mode": "exact-game-build",
            "game_builds": [build.to_mapping() for build in self.game_builds],
        }


@dataclass(frozen=True)
class ModManifest:
    mod_id: str
    name: str
    version: str
    author: str
    loader_api: int
    entrypoint: str
    files: tuple[str, ...]
    description: str = ""
    dependencies: tuple[ModDependency, ...] = ()
    conflicts: tuple[str, ...] = ()
    load_after: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    update: GithubUpdateSource | None = None
    compatibility: ModCompatibility | None = None

    def to_mapping(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": MOD_SCHEMA,
            "format": MOD_FORMAT,
            "mod_id": self.mod_id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "loader_api": self.loader_api,
            "entrypoint": self.entrypoint,
            "files": list(self.files),
            "dependencies": [item.to_mapping() for item in self.dependencies],
            "conflicts": list(self.conflicts),
            "load_after": list(self.load_after),
            "permissions": list(self.permissions),
        }
        if self.update is not None:
            result["update"] = self.update.to_mapping()
        if self.compatibility is not None:
            result["compatibility"] = self.compatibility.to_mapping()
        return result


def _string_id_list(value: Any, label: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ModManifestError(f"{label} must be a list")
    result = tuple(_mod_id(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ModManifestError(f"{label} contains duplicates")
    return result


def _files(value: Any, entrypoint: str) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise ModManifestError("files must be a non-empty list")
    result: list[str] = []
    folded: set[str] = set()
    for raw in value:
        path_text = _require_string(raw, "mod file path", maximum=240)
        if "\\" in path_text or "\x00" in path_text:
            raise ModManifestError(f"unsafe mod file path: {path_text}")
        path = PurePosixPath(path_text)
        if (path.is_absolute() or path.parts[:1] != ("mod",)
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.suffix.lower() not in _ALLOWED_FILE_SUFFIXES):
            raise ModManifestError(f"unsafe or unsupported mod file: {path_text}")
        key = path_text.casefold()
        if key in folded:
            raise ModManifestError(f"duplicate or case-colliding mod file: {path_text}")
        folded.add(key)
        result.append(path_text)
    module_name = entrypoint.split(":", 1)[0]
    module_path = "mod/" + module_name.replace(".", "/")
    if (module_path + ".py" not in result
            and module_path + "/__init__.py" not in result):
        raise ModManifestError("entrypoint module is not declared in files")
    return tuple(result)


def mod_manifest_from_mapping(data: Mapping[str, Any]) -> ModManifest:
    required = {
        "schema", "format", "mod_id", "name", "version", "author",
        "loader_api", "entrypoint", "files",
    }
    optional = {
        "description", "dependencies", "conflicts", "load_after",
        "permissions", "update", "compatibility",
    }
    if set(data) - required - optional or not required.issubset(data):
        raise ModManifestError("mod manifest has unknown or missing fields")
    if data["schema"] != MOD_SCHEMA or data["format"] != MOD_FORMAT:
        raise ModManifestError("unsupported Star Empire mod manifest")
    mod_id = _mod_id(data["mod_id"])
    name = _require_string(data["name"], "name", maximum=80)
    version = _version(data["version"], "version")
    author = _require_string(data["author"], "author", maximum=80)
    description = str(data.get("description", ""))
    if len(description) > 1000:
        raise ModManifestError("description is too long")
    loader_api = data["loader_api"]
    if type(loader_api) is not int or loader_api < 1 or loader_api > 1000:
        raise ModManifestError("loader_api must be a positive integer")
    entrypoint = _require_string(data["entrypoint"], "entrypoint", maximum=160)
    if not _ENTRYPOINT.fullmatch(entrypoint):
        raise ModManifestError("entrypoint must use package.module:callable form")
    files = _files(data["files"], entrypoint)

    raw_dependencies = data.get("dependencies", [])
    if type(raw_dependencies) is not list:
        raise ModManifestError("dependencies must be a list")
    dependencies: list[ModDependency] = []
    for item in raw_dependencies:
        if type(item) is not dict or set(item) != {"mod_id", "version_min"}:
            raise ModManifestError("dependency must contain mod_id and version_min")
        dependencies.append(ModDependency(
            _mod_id(item["mod_id"], "dependency mod_id"),
            _version(item["version_min"], "dependency version_min")))
    dependency_ids = [item.mod_id for item in dependencies]
    if len(set(dependency_ids)) != len(dependency_ids):
        raise ModManifestError("dependencies contains duplicates")

    conflicts = _string_id_list(data.get("conflicts", []), "conflicts")
    load_after = _string_id_list(data.get("load_after", []), "load_after")
    if mod_id in dependency_ids or mod_id in conflicts or mod_id in load_after:
        raise ModManifestError("a mod cannot depend on, conflict with or load after itself")
    if set(dependency_ids) & set(conflicts):
        raise ModManifestError("a dependency cannot also be a conflict")

    raw_permissions = data.get("permissions", [])
    if type(raw_permissions) is not list:
        raise ModManifestError("permissions must be a list")
    permissions = tuple(
        _require_string(item, "permission", maximum=48)
        for item in raw_permissions)
    if (len(set(permissions)) != len(permissions)
            or any(not _PERMISSION.fullmatch(item) for item in permissions)):
        raise ModManifestError("permissions contains duplicates or invalid names")

    update = None
    raw_update = data.get("update")
    if raw_update is not None:
        if (type(raw_update) is not dict
                or set(raw_update) != {"type", "repository", "asset"}
                or raw_update.get("type") != "github-release"):
            raise ModManifestError("update must describe one GitHub release source")
        repository = _require_string(
            raw_update["repository"], "update repository", maximum=160)
        asset = _require_string(raw_update["asset"], "update asset", maximum=120)
        if not _REPOSITORY.fullmatch(repository):
            raise ModManifestError("update repository must use owner/repository form")
        if ("/" in asset or "\\" in asset or ".." in asset
                or not asset.lower().endswith(".semod")):
            raise ModManifestError("update asset must be a .semod filename or pattern")
        update = GithubUpdateSource(repository, asset)

    compatibility = None
    raw_compatibility = data.get("compatibility")
    if raw_compatibility is not None:
        if type(raw_compatibility) is not dict:
            raise ModManifestError("compatibility must be an object")
        mode = raw_compatibility.get("mode")
        if mode == "game-version":
            if set(raw_compatibility) != {"mode", "game_versions"}:
                raise ModManifestError(
                    "game-version compatibility must contain game_versions")
            raw_versions = raw_compatibility["game_versions"]
            if (type(raw_versions) is not list or not raw_versions
                    or len(raw_versions) > 64):
                raise ModManifestError(
                    "compatibility game_versions must be a non-empty list")
            game_versions = tuple(
                _version(item, "game_version") for item in raw_versions)
            if len(set(game_versions)) != len(game_versions):
                raise ModManifestError(
                    "compatibility game_versions contains duplicates")
            compatibility = ModCompatibility(game_versions=game_versions)
        elif mode == "exact-game-build":
            if set(raw_compatibility) != {"mode", "game_builds"}:
                raise ModManifestError(
                    "exact compatibility must contain game_builds")
            raw_builds = raw_compatibility["game_builds"]
            if type(raw_builds) is not list or len(raw_builds) > 64:
                raise ModManifestError(
                    "compatibility game_builds must be a list")
            builds: list[CompatibleGameBuild] = []
            for item in raw_builds:
                if (type(item) is not dict
                        or set(item) != {"game_version", "client_sha256"}):
                    raise ModManifestError(
                        "compatible build must contain game_version and client_sha256")
                game_version = _version(item["game_version"], "game_version")
                client_sha256 = _require_string(
                    item["client_sha256"], "client_sha256", maximum=64).upper()
                if not _SHA256.fullmatch(client_sha256):
                    raise ModManifestError(
                        "client_sha256 must be a SHA-256 digest")
                builds.append(CompatibleGameBuild(
                    game_version, client_sha256))
            identities = {
                (build.game_version, build.client_sha256) for build in builds}
            if len(identities) != len(builds):
                raise ModManifestError(
                    "compatibility game_builds contains duplicates")
            compatibility = ModCompatibility(tuple(builds))
        else:
            raise ModManifestError("unsupported compatibility mode")

    return ModManifest(
        mod_id, name, version, author, loader_api, entrypoint, files,
        description, tuple(dependencies), conflicts, load_after,
        permissions, update, compatibility)


def parse_mod_manifest(payload: bytes) -> ModManifest:
    try:
        data = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModManifestError("mod manifest is not unique-key UTF-8 JSON") from error
    if type(data) is not dict:
        raise ModManifestError("mod manifest must contain one object")
    return mod_manifest_from_mapping(data)
