#!/usr/bin/env python3
"""Back up and restore the complete ephemeral CPA stack state.

Each upload is an immutable, content-addressed tar generation.  SQLite is
copied through its online-backup API; the remaining files are copied only
when their metadata remains stable for the duration of the copy.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from huggingface_hub import HfApi


SCHEMA_VERSION = 1
GENERATION_NAME_RE = re.compile(
    r"state-\d{8}T\d{12}Z-([0-9a-f]{64})\.tar\.gz$"
)
LEGACY_DB_RE = re.compile(r"-([0-9a-f]{12}|[0-9a-f]{64})\.sqlite$")
SHA256_RE = re.compile(r"[0-9a-f]{64}$")
REQUIRED_DIRS = {"cpa", "cpa/auths", "cpa/home", "cpa/plugins", "cpamp"}
REQUIRED_FILES = {
    "cpa/config.yaml",
    "cpamp/data.key",
    "cpamp/usage.sqlite",
}
REQUIRED_DB_TABLES = {"settings", "usage_events"}


def is_allowed_state_file(path: str) -> bool:
    return (
        path in REQUIRED_FILES | {"cpa/management.key"}
        or path.startswith("cpa/auths/")
        or path.startswith("cpa/home/")
        or path.startswith("cpa/plugins/")
    )


def is_allowed_state_directory(path: str) -> bool:
    return (
        path in REQUIRED_DIRS
        or path.startswith("cpa/auths/")
        or path.startswith("cpa/home/")
        or path.startswith("cpa/plugins/")
    )


def log(message: str) -> None:
    print(f"[state] {message}", flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def positive_limit(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def api_client() -> HfApi:
    return HfApi(token=required_env("HF_TOKEN"))


def bucket_id() -> str:
    return os.environ.get("STATE_BUCKET", "04191bw88tk/cr-data").strip()


def generation_prefix() -> str:
    raw = os.environ.get("STATE_SNAPSHOT_PREFIX", "northflank-state")
    return canonical_remote_path(raw, allow_nested=True)


def database_path() -> Path:
    return Path(os.environ.get("USAGE_DB_PATH", "/data/cpamp/usage.sqlite"))


def data_key_path() -> Path:
    return Path(
        os.environ.get("CPA_MANAGER_DATA_KEY_PATH", "/data/cpamp/data.key")
    )


def cpa_data_dir() -> Path:
    return Path(os.environ.get("CPA_DATA_DIR", "/data/cpa"))


def cpa_config_path() -> Path:
    return Path(os.environ.get("CPA_CONFIG", str(cpa_data_dir() / "config.yaml")))


def marker_path() -> Path:
    return database_path().parent / ".last-state.sha256"


def validate_layout() -> None:
    if database_path() != database_path().parent / "usage.sqlite":
        raise RuntimeError("USAGE_DB_PATH must end in usage.sqlite")
    if data_key_path() != database_path().parent / "data.key":
        raise RuntimeError(
            "CPA_MANAGER_DATA_KEY_PATH must be data.key beside USAGE_DB_PATH"
        )
    if cpa_config_path() != cpa_data_dir() / "config.yaml":
        raise RuntimeError("CPA_CONFIG must be CPA_DATA_DIR/config.yaml")


def retry(operation: Callable[[], Any], attempts: int | None = None) -> Any:
    if attempts is None:
        attempts = max(1, int(os.environ.get("STATE_RETRY_ATTEMPTS", "4")))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - network operations are retried
            last_error = error
            if attempt == attempts:
                break
            delay = min(2 ** (attempt - 1), 8)
            log(f"attempt {attempt}/{attempts} failed: {error}; retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"operation failed after {attempts} attempts: {last_error}") from last_error


@contextlib.contextmanager
def backup_lock():
    path = Path(os.environ.get("STATE_BACKUP_LOCK", "/tmp/cpa-state-backup.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - production image is Linux
            yield
            return
        deadline = time.monotonic() + positive_limit("STATE_BACKUP_LOCK_TIMEOUT", 45)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting for the state backup flock")
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_remote_path(value: Any, *, allow_nested: bool = True) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeError(f"invalid remote path: {value!r}")
    if len(value.encode("utf-8")) > positive_limit("STATE_MAX_PATH_BYTES", 1024):
        raise RuntimeError("remote path exceeds STATE_MAX_PATH_BYTES")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"unsafe remote path: {value}")
    canonical = path.as_posix()
    if canonical != value or (not allow_nested and len(path.parts) != 1):
        raise RuntimeError(f"non-canonical remote path: {value}")
    return canonical


def validate_data_key(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"CPA Manager Plus data key is missing: {path}")
    stored = path.read_bytes().strip()
    if not stored:
        raise RuntimeError(f"CPA Manager Plus data key is empty: {path}")
    if len(stored) == 32:
        return
    try:
        encoded = stored.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"invalid CPA Manager Plus data key: {path}") from error
    padding = "=" * (-len(encoded) % 4)
    for altchars in (None, b"-_"):
        try:
            decoded = base64.b64decode(
                encoded + padding,
                altchars=altchars,
                validate=True,
            )
            if len(decoded) == 32:
                return
        except (ValueError, binascii.Error):
            pass
    raise RuntimeError(f"CPA Manager Plus data key must decode to 32 bytes: {path}")


def validate_database(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"SQLite database is missing or empty: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        connection.execute("pragma busy_timeout = 30000")
        result = connection.execute("pragma integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {result}")
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }
        missing = REQUIRED_DB_TABLES - tables
        if missing:
            raise RuntimeError(f"SQLite database is missing required tables: {sorted(missing)}")
    finally:
        connection.close()


def create_sqlite_copy(source: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30
    )
    destination_connection = sqlite3.connect(str(destination), timeout=30)
    try:
        source_connection.execute("pragma busy_timeout = 30000")
        source_connection.backup(destination_connection, pages=256, sleep=0.05)
        destination_connection.commit()
        mode = destination_connection.execute("pragma journal_mode = DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise RuntimeError(f"unexpected snapshot journal mode: {mode}")
    finally:
        destination_connection.close()
        source_connection.close()
    validate_database(destination)


def stable_file_signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"state contains a non-regular file: {path}")
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)


def copy_stable_file(source: Path, destination: Path, attempts: int = 4) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        before = stable_file_signature(source)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, 1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            after = stable_file_signature(source)
            if before == after and temporary.stat().st_size == before[0]:
                os.chmod(temporary, source.stat(follow_symlinks=False).st_mode & 0o777)
                os.replace(temporary, destination)
                return
        except FileNotFoundError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
        if attempt < attempts:
            time.sleep(0.1 * attempt)
    raise RuntimeError(f"file changed while being snapshotted: {source}")


def tree_inventory(root: Path) -> dict[str, tuple[str, int, int, int, int]]:
    inventory: dict[str, tuple[str, int, int, int, int]] = {}
    if not root.exists():
        return inventory
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            inventory[relative] = (
                "d",
                0,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_ino,
            )
        elif stat.S_ISREG(info.st_mode):
            inventory[relative] = (
                "f",
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_ino,
            )
        else:
            raise RuntimeError(f"state tree contains an unsupported entry: {path}")
    return inventory


def copy_stable_tree(source: Path, destination: Path, attempts: int = 4) -> None:
    for attempt in range(1, attempts + 1):
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True, exist_ok=True)
        before = tree_inventory(source)
        try:
            for relative, entry in before.items():
                source_path = source / relative
                target_path = destination / relative
                if entry[0] == "d":
                    target_path.mkdir(parents=True, exist_ok=True)
                else:
                    copy_stable_file(source_path, target_path)
            after = tree_inventory(source)
            if before == after:
                return
        except (FileNotFoundError, RuntimeError):
            if attempt == attempts:
                raise
        if attempt < attempts:
            time.sleep(0.2 * attempt)
    raise RuntimeError(f"directory changed while being snapshotted: {source}")


def cpa_allowlisted_inventory() -> dict[str, tuple[Any, ...]]:
    inventory: dict[str, tuple[Any, ...]] = {}
    for name, path in (
        ("config.yaml", cpa_config_path()),
        ("management.key", cpa_data_dir() / "management.key"),
    ):
        if os.path.lexists(path):
            inventory[name] = ("f", *stable_file_signature(path))
    for directory_name in ("auths", "home", "plugins"):
        root = cpa_data_dir() / directory_name
        if os.path.lexists(root):
            info = root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"CPA allowlisted path is not a directory: {root}")
            inventory[directory_name] = (
                "d",
                0,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_ino,
            )
            for relative, signature in tree_inventory(root).items():
                inventory[f"{directory_name}/{relative}"] = signature
    return inventory


def copy_cpa_allowlisted_state(destination: Path, attempts: int = 4) -> None:
    for attempt in range(1, attempts + 1):
        shutil.rmtree(destination, ignore_errors=True)
        for name in ("auths", "home", "plugins"):
            (destination / name).mkdir(parents=True, exist_ok=True)
        before = cpa_allowlisted_inventory()
        try:
            copy_stable_file(cpa_config_path(), destination / "config.yaml")
            management_key = cpa_data_dir() / "management.key"
            if os.path.lexists(management_key):
                copy_stable_file(management_key, destination / "management.key")
            for name in ("auths", "home", "plugins"):
                copy_stable_tree(cpa_data_dir() / name, destination / name)
            after = cpa_allowlisted_inventory()
            if before == after:
                return
        except (FileNotFoundError, RuntimeError):
            if attempt == attempts:
                raise
        if attempt < attempts:
            time.sleep(0.2 * attempt)
    raise RuntimeError("CPA allowlisted state changed throughout all snapshot attempts")


def build_manifest(state_root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    directories: list[str] = []
    maximum_file = positive_limit("STATE_MAX_FILE_BYTES", 1024**3)
    maximum_total = positive_limit("STATE_MAX_UNPACKED_BYTES", 2 * 1024**3)
    total = 0
    for path in sorted(state_root.rglob("*")):
        relative = safe_state_relative(path.relative_to(state_root).as_posix())
        if path.is_dir():
            if not is_allowed_state_directory(relative):
                raise RuntimeError(f"state contains a non-allowlisted directory: {relative}")
            directories.append(relative)
        elif path.is_file():
            if not is_allowed_state_file(relative):
                raise RuntimeError(f"state contains a non-allowlisted file: {relative}")
            info = path.stat()
            if info.st_size > maximum_file:
                raise RuntimeError(f"state file exceeds STATE_MAX_FILE_BYTES: {relative}")
            total += info.st_size
            if total > maximum_total:
                raise RuntimeError("state exceeds STATE_MAX_UNPACKED_BYTES")
            files.append(
                {
                    "path": relative,
                    "size": info.st_size,
                    "sha256": sha256_file(path),
                    "mode": stat.S_IMODE(info.st_mode),
                }
            )
        else:
            raise RuntimeError(f"unsupported staged entry: {path}")
    if len(files) + len(directories) + 2 > positive_limit("STATE_MAX_MEMBERS", 20000):
        raise RuntimeError("state exceeds STATE_MAX_MEMBERS")
    file_names = {entry["path"] for entry in files}
    if not REQUIRED_FILES.issubset(file_names):
        raise RuntimeError(f"snapshot is missing required files: {REQUIRED_FILES - file_names}")
    if not REQUIRED_DIRS.issubset(set(directories)):
        raise RuntimeError(
            f"snapshot is missing required directories: {REQUIRED_DIRS - set(directories)}"
        )
    content = {"directories": directories, "files": files}
    content_sha256 = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": content_sha256,
        **content,
    }


def stage_current_state(state_root: Path) -> dict[str, Any]:
    manager_root = state_root / "cpamp"
    cpa_root = state_root / "cpa"
    manager_root.mkdir(parents=True)

    create_sqlite_copy(database_path(), manager_root / "usage.sqlite")
    copy_stable_file(data_key_path(), manager_root / "data.key")
    validate_data_key(manager_root / "data.key")
    copy_cpa_allowlisted_state(cpa_root)
    return build_manifest(state_root)


def write_archive(state_root: Path, manifest: dict[str, Any], archive: Path) -> None:
    manifest_path = state_root.parent / "manifest.json"
    encoded_manifest = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded_manifest) > positive_limit("STATE_MAX_MANIFEST_BYTES", 8 * 1024**2):
        raise RuntimeError("manifest exceeds STATE_MAX_MANIFEST_BYTES")
    manifest_path.write_bytes(encoded_manifest)
    with tarfile.open(archive, "w:gz", compresslevel=6) as bundle:
        bundle.add(manifest_path, arcname="manifest.json", recursive=False)
        bundle.add(state_root, arcname="state", recursive=True)


def safe_member_path(name: str) -> PurePosixPath:
    if not isinstance(name, str) or "\\" in name or "\x00" in name:
        raise RuntimeError(f"invalid archive path: {name!r}")
    if len(name.encode("utf-8")) > positive_limit("STATE_MAX_PATH_BYTES", 1024):
        raise RuntimeError("archive path exceeds STATE_MAX_PATH_BYTES")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != name.rstrip("/")
    ):
        raise RuntimeError(f"unsafe archive path: {name}")
    if path.parts[0] not in {"manifest.json", "state"}:
        raise RuntimeError(f"unexpected archive path: {name}")
    if path.parts[0] == "manifest.json" and len(path.parts) != 1:
        raise RuntimeError(f"unexpected manifest path: {name}")
    return path


def extract_archive(archive: Path, destination: Path) -> dict[str, Any]:
    archive_limit = positive_limit("STATE_MAX_ARCHIVE_BYTES", 512 * 1024**2)
    if not archive.is_file() or archive.stat().st_size <= 0:
        raise RuntimeError("archive is missing or empty")
    if archive.stat().st_size > archive_limit:
        raise RuntimeError("archive exceeds STATE_MAX_ARCHIVE_BYTES")
    seen: set[str] = set()
    maximum = positive_limit("STATE_MAX_UNPACKED_BYTES", 2 * 1024**3)
    maximum_members = positive_limit("STATE_MAX_MEMBERS", 20000)
    maximum_file = positive_limit("STATE_MAX_FILE_BYTES", 1024**3)
    maximum_manifest = positive_limit("STATE_MAX_MANIFEST_BYTES", 8 * 1024**2)
    total = 0
    member_count = 0
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            member_count += 1
            if member_count > maximum_members:
                raise RuntimeError("archive exceeds STATE_MAX_MEMBERS")
            relative = safe_member_path(member.name)
            normalized = relative.as_posix().rstrip("/")
            if normalized in seen:
                raise RuntimeError(f"duplicate archive entry: {member.name}")
            seen.add(normalized)
            if member.isdir():
                target_directory = destination / relative
                target_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(target_directory, 0o700)
                continue
            if not member.isfile():
                raise RuntimeError(f"unsupported archive member: {member.name}")
            if member.size < 0 or member.size > maximum_file:
                raise RuntimeError(f"archive member exceeds STATE_MAX_FILE_BYTES: {member.name}")
            if normalized == "manifest.json" and member.size > maximum_manifest:
                raise RuntimeError("manifest exceeds STATE_MAX_MANIFEST_BYTES")
            total += member.size
            if total > maximum:
                raise RuntimeError("archive exceeds STATE_MAX_UNPACKED_BYTES")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target.parent, 0o700)
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"could not read archive member: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            if target.stat().st_size != member.size:
                raise RuntimeError(f"truncated archive member: {member.name}")
            os.chmod(target, 0o600)

    manifest_path = destination / "manifest.json"
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_json_keys,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("archive manifest is missing or invalid") from error
    validate_extracted_state(destination / "state", manifest)
    return manifest


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def validate_extracted_state(state_root: Path, manifest: dict[str, Any]) -> None:
    if set(manifest) != {
        "schema",
        "created_at",
        "content_sha256",
        "directories",
        "files",
    }:
        raise RuntimeError("manifest has missing or unexpected top-level fields")
    if manifest.get("schema") != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported state schema: {manifest.get('schema')}")
    if (
        not isinstance(manifest.get("created_at"), str)
        or len(manifest["created_at"]) > 64
        or not isinstance(manifest.get("content_sha256"), str)
        or SHA256_RE.fullmatch(manifest["content_sha256"]) is None
    ):
        raise RuntimeError("manifest identity fields are invalid")
    files = manifest.get("files")
    directories = manifest.get("directories")
    if not isinstance(files, list) or not isinstance(directories, list):
        raise RuntimeError("manifest files/directories are invalid")
    if len(files) + len(directories) > positive_limit("STATE_MAX_MEMBERS", 20000):
        raise RuntimeError("manifest exceeds STATE_MAX_MEMBERS")
    expected_files: set[str] = set()
    for entry in files:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "size", "sha256", "mode"}
            or not isinstance(entry.get("path"), str)
        ):
            raise RuntimeError("manifest file entry is invalid")
        relative = safe_state_relative(entry["path"])
        if not is_allowed_state_file(relative):
            raise RuntimeError(f"manifest contains a non-allowlisted file: {relative}")
        if relative in expected_files:
            raise RuntimeError(f"duplicate manifest file: {relative}")
        expected_files.add(relative)
        size = entry.get("size")
        digest = entry.get("sha256")
        mode = entry.get("mode")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > positive_limit("STATE_MAX_FILE_BYTES", 1024**3)
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o777
        ):
            raise RuntimeError(f"manifest metadata is invalid: {relative}")
        path = state_root / PurePosixPath(relative)
        if not path.is_file() or path.stat().st_size != size:
            raise RuntimeError(f"snapshot file is missing or has the wrong size: {relative}")
        if sha256_file(path) != digest:
            raise RuntimeError(f"snapshot file checksum mismatch: {relative}")
    expected_directories: set[str] = set()
    for value in directories:
        relative = safe_state_relative(value)
        if not is_allowed_state_directory(relative):
            raise RuntimeError(f"manifest contains a non-allowlisted directory: {relative}")
        if relative in expected_directories:
            raise RuntimeError(f"duplicate manifest directory: {relative}")
        expected_directories.add(relative)
    actual_files = {
        path.relative_to(state_root).as_posix()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    actual_directories = {
        path.relative_to(state_root).as_posix()
        for path in state_root.rglob("*")
        if path.is_dir()
    }
    if actual_files != expected_files:
        raise RuntimeError("archive and manifest file lists differ")
    if actual_directories != expected_directories:
        raise RuntimeError("archive and manifest directory lists differ")
    if not REQUIRED_FILES.issubset(expected_files):
        raise RuntimeError("archive is missing required state files")
    if not REQUIRED_DIRS.issubset(expected_directories):
        raise RuntimeError("archive is missing required state directories")

    content = {"directories": directories, "files": files}
    calculated = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if calculated != manifest.get("content_sha256"):
        raise RuntimeError("manifest content checksum mismatch")
    validate_database(state_root / "cpamp" / "usage.sqlite")
    validate_data_key(state_root / "cpamp" / "data.key")
    config = state_root / "cpa" / "config.yaml"
    if config.stat().st_size == 0:
        raise RuntimeError("CPA config is empty")


def safe_state_relative(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError("manifest path is not a string")
    if "\\" in value or "\x00" in value:
        raise RuntimeError(f"invalid manifest path: {value!r}")
    if len(value.encode("utf-8")) > positive_limit("STATE_MAX_PATH_BYTES", 1024):
        raise RuntimeError("manifest path exceeds STATE_MAX_PATH_BYTES")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError(f"unsafe manifest path: {value}")
    return path.as_posix()


def list_generations(api: HfApi) -> list[Any]:
    prefix = generation_prefix()
    items = api.list_bucket_tree(bucket_id(), prefix=prefix, recursive=True)
    generations: list[Any] = []
    for item in items:
        raw_path = getattr(item, "path", "")
        path = canonical_remote_path(raw_path)
        parsed = PurePosixPath(path)
        if parsed.parent.as_posix() != prefix:
            continue
        if GENERATION_NAME_RE.fullmatch(parsed.name) is None:
            continue
        generations.append(item)
    return sorted(generations, key=lambda item: item.path)


def remote_generation_digest(path: str) -> str:
    canonical = canonical_remote_path(path)
    parsed = PurePosixPath(canonical)
    if parsed.parent.as_posix() != generation_prefix():
        raise RuntimeError(f"generation is outside the exact snapshot prefix: {path}")
    match = GENERATION_NAME_RE.fullmatch(parsed.name)
    if match is None:
        raise RuntimeError(f"generation filename has no SHA-256: {path}")
    return match.group(1)


def download_file(api: HfApi, remote_path: str, local_path: Path) -> None:
    remote_path = canonical_remote_path(remote_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.unlink(missing_ok=True)
    api.download_bucket_files(
        bucket_id(),
        [(remote_path, local_path)],
        raise_on_missing_files=True,
    )


def install_state(source: Path, content_sha256: str) -> None:
    for path in [source, *source.rglob("*")]:
        info = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o700)
        elif stat.S_ISREG(info.st_mode):
            os.chmod(path, 0o600)
        else:
            raise RuntimeError(f"restored state contains an unsupported entry: {path}")
    targets = [
        (source / "cpamp", database_path().parent),
        (source / "cpa", cpa_data_dir()),
    ]
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    suffix = uuid.uuid4().hex
    try:
        for source_dir, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            old = target.with_name(f".{target.name}.pre-restore-{suffix}")
            if target.exists():
                os.replace(target, old)
                backups.append((target, old))
            os.replace(source_dir, target)
            installed.append(target)
    except Exception:
        for target in reversed(installed):
            shutil.rmtree(target, ignore_errors=True)
        for target, old in reversed(backups):
            if old.exists():
                os.replace(old, target)
        raise
    for _, old in backups:
        shutil.rmtree(old, ignore_errors=True)
    for suffix_name in ("-wal", "-shm", "-journal"):
        Path(f"{database_path()}{suffix_name}").unlink(missing_ok=True)
    marker_path().write_text(content_sha256 + "\n", encoding="ascii")
    os.chmod(marker_path(), 0o600)


def restore_generation(api: HfApi, item: Any, work_root: Path) -> str:
    remote_path = canonical_remote_path(getattr(item, "path", ""))
    remote_size = getattr(item, "size", None)
    if (
        not isinstance(remote_size, int)
        or isinstance(remote_size, bool)
        or remote_size <= 0
        or remote_size > positive_limit("STATE_MAX_ARCHIVE_BYTES", 512 * 1024**2)
    ):
        raise RuntimeError(f"invalid remote generation size: {remote_path}")
    archive = work_root / "generation.tar.gz"
    retry(lambda: download_file(api, remote_path, archive))
    if archive.stat().st_size != remote_size:
        raise RuntimeError(
            f"downloaded generation size mismatch: remote={remote_size}, "
            f"local={archive.stat().st_size}"
        )
    actual_digest = sha256_file(archive)
    expected_digest = remote_generation_digest(remote_path)
    if actual_digest != expected_digest:
        raise RuntimeError(
            f"archive SHA-256 mismatch: filename={expected_digest}, actual={actual_digest}"
        )
    extraction = work_root / "extract"
    extraction.mkdir()
    manifest = extract_archive(archive, extraction)
    install_state(extraction / "state", manifest["content_sha256"])
    return manifest["content_sha256"]


def bucket_inventory(api: HfApi) -> dict[str, Any]:
    items = api.list_bucket_tree(bucket_id(), prefix=None, recursive=True)
    inventory: dict[str, Any] = {}
    for item in items:
        path = canonical_remote_path(getattr(item, "path", ""))
        if path in inventory:
            raise RuntimeError(f"duplicate remote Bucket path: {path}")
        inventory[path] = item
    return inventory


def remote_file_size(item: Any, path: str, maximum: int) -> int:
    size = getattr(item, "size", None)
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or size > maximum
    ):
        raise RuntimeError(f"remote file has an invalid size: {path}")
    return size


def legacy_database_candidates(inventory: dict[str, Any]) -> list[str]:
    prefix = canonical_remote_path(
        os.environ.get("STATE_LEGACY_DB_PREFIX", "cpamp-snapshots")
    )
    snapshots: list[str] = []
    for path, item in inventory.items():
        parsed = PurePosixPath(path)
        if (
            parsed.parent.as_posix() == prefix
            and parsed.name.startswith("usage-")
            and parsed.name.endswith(".sqlite")
            and LEGACY_DB_RE.search(parsed.name) is not None
            and getattr(item, "size", None) is not None
        ):
            snapshots.append(path)
    snapshots.sort(reverse=True)
    if "usage.sqlite" in inventory and getattr(inventory["usage.sqlite"], "size", None) is not None:
        snapshots.append("usage.sqlite")
    return snapshots


def is_legacy_cpa_path(path: str) -> bool:
    return (
        path in {"cpa/config.yaml", "cpa/management.key"}
        or path.startswith("cpa/auths/")
        or path.startswith("cpa/home/")
        or path.startswith("cpa/plugins/")
    )


def legacy_cpa_signature(inventory: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    signature: dict[str, tuple[Any, ...]] = {}
    for path, item in inventory.items():
        if not is_legacy_cpa_path(path) or getattr(item, "size", None) is None:
            continue
        signature[path] = remote_item_signature(item)
    return signature


def remote_item_signature(item: Any) -> tuple[Any, ...]:
    return (
        getattr(item, "size", None),
        getattr(item, "xet_hash", None),
        getattr(item, "blob_id", None),
        getattr(item, "etag", None),
        getattr(item, "mtime", None),
        getattr(item, "uploaded_at", None),
    )


def restore_legacy(api: HfApi, work_root: Path) -> bool:
    inventory = retry(lambda: bucket_inventory(api))
    candidates = legacy_database_candidates(inventory)
    database = work_root / "legacy-usage.sqlite"
    selected = ""
    failures: list[str] = []
    for remote in candidates:
        try:
            expected_size = remote_file_size(
                inventory[remote],
                remote,
                positive_limit("STATE_MAX_FILE_BYTES", 1024**3),
            )
            retry(lambda remote=remote: download_file(api, remote, database))
            if database.stat().st_size != expected_size:
                raise RuntimeError("legacy database download size mismatch")
            validate_database(database)
            match = LEGACY_DB_RE.search(remote)
            if match is not None and not sha256_file(database).startswith(match.group(1)):
                raise RuntimeError("legacy database checksum mismatch")
            selected = remote
            break
        except Exception as error:  # noqa: BLE001 - try an older legacy snapshot
            failures.append(f"{remote}: {error}")
            database.unlink(missing_ok=True)
    if not selected:
        if candidates:
            raise RuntimeError("no valid legacy database; " + "; ".join(failures))
        if env_bool("STATE_REQUIRE_EXISTING", True):
            raise RuntimeError(
                "STATE_REQUIRE_EXISTING=true but no state generation or legacy database exists"
            )
        log("no prior state generation found; STATE_REQUIRE_EXISTING=false permits empty state")
        return False

    key = work_root / "legacy-data.key"
    key_remote = ""
    for remote in ("data.key", "cpamp/data.key", "cpa-manager/data.key"):
        item = inventory.get(remote)
        if item is None or getattr(item, "size", None) is None:
            continue
        try:
            expected_size = remote_file_size(item, remote, 4096)
            retry(lambda remote=remote: download_file(api, remote, key))
            if key.stat().st_size != expected_size:
                raise RuntimeError("legacy data.key download size mismatch")
            validate_data_key(key)
            key_remote = remote
            break
        except Exception as error:  # noqa: BLE001 - try the next historical layout
            failures.append(f"{remote}: {error}")
            key.unlink(missing_ok=True)
    if not key_remote:
        detail = "; ".join(failures)
        raise RuntimeError(
            "legacy Manager database exists but no valid matching data.key exists"
            + (f"; {detail}" if detail else "")
        )

    state = work_root / "legacy-state"
    (state / "cpamp").mkdir(parents=True)
    (state / "cpa" / "auths").mkdir(parents=True)
    (state / "cpa" / "home").mkdir(parents=True)
    (state / "cpa" / "plugins").mkdir(parents=True)
    os.replace(database, state / "cpamp" / "usage.sqlite")
    os.replace(key, state / "cpamp" / "data.key")

    allowed: list[tuple[str, Path]] = []
    before_cpa = legacy_cpa_signature(inventory)
    if "cpa/config.yaml" not in before_cpa:
        raise RuntimeError("legacy state is missing cpa/config.yaml")
    for remote, item in inventory.items():
        if not is_legacy_cpa_path(remote) or getattr(item, "size", None) is None:
            continue
        remote_file_size(
            item,
            remote,
            positive_limit("STATE_MAX_FILE_BYTES", 1024**3),
        )
        relative = PurePosixPath(remote).relative_to("cpa")
        target = state / "cpa" / Path(*relative.parts)
        allowed.append((remote, target))
    if allowed:
        retry(
            lambda: api.download_bucket_files(
                bucket_id(), allowed, raise_on_missing_files=True
            )
        )
    after_inventory = retry(lambda: bucket_inventory(api))
    if before_cpa != legacy_cpa_signature(after_inventory):
        raise RuntimeError("legacy CPA allowlisted inventory changed during migration")
    for remote in (selected, key_remote):
        if remote not in after_inventory or remote_item_signature(
            inventory[remote]
        ) != remote_item_signature(after_inventory[remote]):
            raise RuntimeError(f"legacy remote object changed during migration: {remote}")
    for remote, local in allowed:
        expected_size = remote_file_size(
            inventory[remote],
            remote,
            positive_limit("STATE_MAX_FILE_BYTES", 1024**3),
        )
        if not local.is_file() or local.stat().st_size != expected_size:
            raise RuntimeError(f"legacy CPA file download size mismatch: {remote}")
    install_state(state, sha256_file(state / "cpamp" / "usage.sqlite"))
    log(f"imported legacy database {selected} with {key_remote}")
    return True


def restore() -> bool:
    validate_layout()
    api = api_client()
    generations = retry(lambda: list_generations(api))
    common_parent = Path(
        os.path.commonpath(
            [str(database_path().parent.resolve()), str(cpa_data_dir().resolve())]
        )
    )
    common_parent.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    if generations:
        for item in reversed(generations):
            with tempfile.TemporaryDirectory(
                prefix=".state-restore-", dir=common_parent
            ) as temporary:
                try:
                    content_digest = restore_generation(api, item, Path(temporary))
                    log(
                        f"restored {item.path} "
                        f"(content sha256={content_digest[:12]})"
                    )
                    return True
                except Exception as error:  # noqa: BLE001 - reject and fall back
                    failures.append(f"{item.path}: {error}")
                    log(f"rejected generation {item.path}: {error}")
        raise RuntimeError("no valid state generation; " + "; ".join(failures))

    with tempfile.TemporaryDirectory(
        prefix=".legacy-restore-", dir=common_parent
    ) as temporary:
        return restore_legacy(api, Path(temporary))


def publish_archive(api: HfApi, archive: Path, remote_path: str) -> None:
    remote_path = canonical_remote_path(remote_path)
    expected_size = archive.stat().st_size
    if expected_size <= 0 or expected_size > positive_limit(
        "STATE_MAX_ARCHIVE_BYTES", 512 * 1024**2
    ):
        raise RuntimeError("local generation has an invalid archive size")

    def ensure_uploaded() -> None:
        existing = [item for item in list_generations(api) if item.path == remote_path]
        if existing:
            if existing[0].size != expected_size:
                raise RuntimeError("immutable generation path already has a different size")
            return
        api.batch_bucket_files(bucket_id(), add=[(archive, remote_path)])

    retry(ensure_uploaded)
    remote = [item for item in list_generations(api) if item.path == remote_path]
    if not remote or remote[0].size != expected_size:
        raise RuntimeError("uploaded generation could not be found with the expected size")

    if env_bool("STATE_VERIFY_UPLOAD", True):
        verification = archive.with_name(f".{archive.name}.verify")
        try:
            retry(lambda: download_file(api, remote_path, verification))
            if sha256_file(verification) != sha256_file(archive):
                raise RuntimeError("downloaded generation checksum does not match upload")
            extracted = archive.parent / "verify-extracted"
            extracted.mkdir()
            extract_archive(verification, extracted)
        finally:
            verification.unlink(missing_ok=True)


def prune_generations(api: HfApi) -> None:
    keep = max(3, int(os.environ.get("STATE_SNAPSHOT_KEEP", "12")))
    generations = list_generations(api)
    stale = [canonical_remote_path(item.path) for item in generations[:-keep]]
    if not stale:
        return
    try:
        api.batch_bucket_files(bucket_id(), delete=stale)
        log(f"pruned {len(stale)} expired generation(s); retained {keep}")
    except Exception as error:  # noqa: BLE001 - retention is non-critical
        log(f"generation pruning failed: {error}")


def _backup_locked(force: bool = False) -> bool:
    validate_layout()
    if not database_path().is_file():
        raise RuntimeError(f"database does not exist: {database_path()}")
    with tempfile.TemporaryDirectory(prefix="cpa-state-") as temporary:
        work = Path(temporary)
        state = work / "state"
        state.mkdir()
        manifest = stage_current_state(state)
        content_digest = manifest["content_sha256"]
        previous_digest = ""
        if marker_path().is_file():
            previous_digest = marker_path().read_text(encoding="ascii").strip()
        if not force and content_digest == previous_digest:
            log("persistent state unchanged; generation skipped")
            return False

        archive = work / "state.tar.gz"
        write_archive(state, manifest, archive)
        archive_digest = sha256_file(archive)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        remote_path = (
            f"{generation_prefix()}/state-{timestamp}-{archive_digest}.tar.gz"
        )
        api = api_client()
        publish_archive(api, archive, remote_path)
        marker_path().parent.mkdir(parents=True, exist_ok=True)
        marker_path().write_text(content_digest + "\n", encoding="ascii")
        os.chmod(marker_path(), 0o600)
        log(
            f"uploaded and verified {remote_path} "
            f"({archive.stat().st_size} bytes)"
        )
        prune_generations(api)
        return True


def backup(force: bool = False) -> bool:
    with backup_lock():
        return _backup_locked(force=force)


def watch(interval: int) -> None:
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log(f"periodic state worker started (interval={interval}s)")
    while not stop_event.wait(interval):
        try:
            backup(force=False)
        except Exception as error:  # noqa: BLE001 - keep trying on the next interval
            log(f"periodic backup failed: {error}")
    log("periodic state worker stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("restore")
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--force", action="store_true")
    watch_parser = commands.add_parser("watch")
    watch_parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("STATE_SNAPSHOT_INTERVAL_SECONDS", "60")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "restore":
            restore()
        elif args.command == "backup":
            backup(force=args.force)
        else:
            watch(max(30, args.interval))
        return 0
    except Exception as error:  # noqa: BLE001 - concise fatal startup error
        log(f"fatal: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
