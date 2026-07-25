#!/usr/bin/env python3
"""Create and restore consistent CPA Manager Plus SQLite snapshots."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi


# Older snapshots used a 12-hex SHA-256 prefix; new snapshots carry the full
# digest. Accept both so rolling upgrades retain their recovery history.
SNAPSHOT_DIGEST_PATTERN = re.compile(r"-([0-9a-f]{12}|[0-9a-f]{64})\.sqlite$")


def log(message: str) -> None:
    print(f"[snapshot] {message}", flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def api_client() -> HfApi:
    return HfApi(token=required_env("HF_TOKEN"))


def bucket_id() -> str:
    return required_env("CPAMP_SNAPSHOT_BUCKET")


def snapshot_prefix() -> str:
    return os.environ.get("CPAMP_SNAPSHOT_PREFIX", "cpamp-snapshots").strip("/")


def database_path() -> Path:
    return Path(os.environ.get("USAGE_DB_PATH", "/var/lib/cpamp/usage.sqlite"))


def data_key_path() -> Path:
    return Path(os.environ.get("CPA_MANAGER_DATA_KEY_PATH", "/data/data.key"))


def state_path() -> Path:
    return database_path().parent / ".last-snapshot.sha256"


def retry(operation, attempts: int = 3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - retry network and Hub errors
            last_error = error
            if attempt == attempts:
                break
            delay = min(2 ** (attempt - 1), 8)
            log(f"attempt {attempt}/{attempts} failed: {error}; retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"operation failed after {attempts} attempts: {last_error}") from last_error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_data_key() -> None:
    path = data_key_path()
    if not path.is_file():
        raise RuntimeError(f"CPA Manager Plus data key is missing: {path}")

    stored = path.read_bytes().strip()
    if not stored:
        raise RuntimeError(f"CPA Manager Plus data key is empty: {path}")

    # Match CPAMP's parseStoredDataKey: the file may contain raw 32-byte
    # material or a standard/raw-standard/raw-URL base64 representation of it.
    if len(stored) == 32:
        return

    try:
        encoded = stored.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"CPA Manager Plus data key has an invalid format: {path}") from error

    padding = "=" * (-len(encoded) % 4)
    decoders = (
        lambda: base64.b64decode(encoded + padding, validate=True),
        lambda: base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        ),
    )
    for decode in decoders:
        try:
            if len(decode()) == 32:
                return
        except (ValueError, binascii.Error):
            continue
    raise RuntimeError(
        f"CPA Manager Plus data key must decode to exactly 32 bytes: {path}"
    )


def validate_database(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"snapshot is missing or empty: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        result = connection.execute("pragma integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {result}")
        table_count = connection.execute(
            "select count(*) from sqlite_master where type = 'table'"
        ).fetchone()[0]
        if table_count == 0:
            raise RuntimeError("snapshot contains no tables")
    finally:
        connection.close()


def list_snapshots(api: HfApi) -> list:
    prefix = snapshot_prefix()
    items = api.list_bucket_tree(bucket_id(), prefix=prefix, recursive=True)
    return sorted(
        [
            item
            for item in items
            if getattr(item, "path", "").startswith(f"{prefix}/usage-")
            and getattr(item, "path", "").endswith(".sqlite")
        ],
        key=lambda item: item.path,
    )


def expected_snapshot_digest(remote_path: str) -> str:
    match = SNAPSHOT_DIGEST_PATTERN.search(remote_path)
    if match is None:
        raise RuntimeError(f"snapshot filename has no SHA-256 suffix: {remote_path}")
    return match.group(1)


def restore() -> None:
    db_path = database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = db_path.with_name(f".{db_path.name}.restore-{uuid.uuid4().hex}.tmp")
    api = api_client()

    try:
        snapshots = retry(lambda: list_snapshots(api))
        if snapshots:
            candidates = list(reversed(snapshots))
        else:
            # One-time migration path from the original direct-on-volume database.
            candidates = ["usage.sqlite"]

        failures: list[str] = []
        for source in candidates:
            source_path = source.path if hasattr(source, "path") else str(source)
            temporary.unlink(missing_ok=True)
            Path(f"{temporary}-wal").unlink(missing_ok=True)
            Path(f"{temporary}-shm").unlink(missing_ok=True)
            try:
                retry(
                    lambda source=source: api.download_bucket_files(
                        bucket_id(),
                        [(source, temporary)],
                        raise_on_missing_files=True,
                    )
                )
                validate_database(temporary)
                digest = sha256_file(temporary)
                if source_path != "usage.sqlite":
                    expected_digest = expected_snapshot_digest(source_path)
                    if not digest.startswith(expected_digest):
                        raise RuntimeError(
                            "snapshot SHA-256 mismatch: "
                            f"filename={expected_digest}, actual={digest[:len(expected_digest)]}"
                        )

                # Restore runs before CPAMP starts, so no connection can be
                # using these files. Never let a stale hot journal or WAL from
                # an earlier in-container run attach to the replacement DB.
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(f"{db_path}{suffix}").unlink(missing_ok=True)
                os.replace(temporary, db_path)
                state_path().write_text(f"{digest}\n", encoding="ascii")
                log(
                    f"restored {source_path} "
                    f"({db_path.stat().st_size} bytes, sha256={digest[:12]})"
                )
                return
            except Exception as error:  # noqa: BLE001 - reject and try an older generation
                failures.append(f"{source_path}: {error}")
                log(f"rejected snapshot {source_path}: {error}")

        raise RuntimeError("no valid SQLite snapshot found; " + "; ".join(failures))
    finally:
        temporary.unlink(missing_ok=True)
        Path(f"{temporary}-wal").unlink(missing_ok=True)
        Path(f"{temporary}-shm").unlink(missing_ok=True)


def create_consistent_copy(source: Path, destination: Path) -> None:
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
        result = destination_connection.execute("pragma integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"snapshot integrity_check failed: {result}")
    finally:
        destination_connection.close()
        source_connection.close()


def backup(force: bool = False) -> bool:
    db_path = database_path()
    if not db_path.is_file():
        raise RuntimeError(f"database does not exist: {db_path}")

    temporary = db_path.with_name(f".{db_path.name}.snapshot-{uuid.uuid4().hex}.tmp")
    try:
        create_consistent_copy(db_path, temporary)
        validate_database(temporary)
        digest = sha256_file(temporary)

        previous_digest = ""
        if state_path().is_file():
            previous_digest = state_path().read_text(encoding="ascii").strip()
        if not force and digest == previous_digest:
            log("database unchanged; snapshot skipped")
            return False

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        remote_path = f"{snapshot_prefix()}/usage-{timestamp}-{digest}.sqlite"
        api = api_client()

        def upload_and_verify() -> None:
            api.batch_bucket_files(bucket_id(), add=[(temporary, remote_path)])
            uploaded = [item for item in list_snapshots(api) if item.path == remote_path]
            if not uploaded or uploaded[0].size != temporary.stat().st_size:
                raise RuntimeError("uploaded snapshot could not be verified")

        retry(upload_and_verify)
        state_path().write_text(f"{digest}\n", encoding="ascii")
        log(f"uploaded {remote_path} ({temporary.stat().st_size} bytes)")

        keep = max(2, int(os.environ.get("CPAMP_SNAPSHOT_KEEP", "8")))
        snapshots = list_snapshots(api)
        stale = [item.path for item in snapshots[:-keep]]
        if stale:
            try:
                api.batch_bucket_files(bucket_id(), delete=stale)
                log(f"pruned {len(stale)} old snapshot(s)")
            except Exception as error:  # noqa: BLE001 - pruning is non-critical
                log(f"snapshot pruning failed: {error}")
        return True
    finally:
        temporary.unlink(missing_ok=True)
        Path(f"{temporary}-wal").unlink(missing_ok=True)
        Path(f"{temporary}-shm").unlink(missing_ok=True)
        Path(f"{temporary}-journal").unlink(missing_ok=True)


def watch(interval: int) -> None:
    stop_event = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log(f"periodic snapshot worker started (interval={interval}s)")
    while not stop_event.wait(interval):
        try:
            backup(force=False)
        except Exception as error:  # noqa: BLE001 - keep worker alive for the next retry
            log(f"periodic snapshot failed: {error}")
    log("periodic snapshot worker stopped")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("restore")
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--force", action="store_true")
    watch_parser = subparsers.add_parser("watch")
    watch_parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("CPAMP_SNAPSHOT_INTERVAL_SECONDS", "120")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_data_key()
        if args.command == "restore":
            restore()
        elif args.command == "backup":
            backup(force=args.force)
        else:
            watch(max(30, args.interval))
        return 0
    except Exception as error:  # noqa: BLE001 - print one concise fatal error
        log(f"fatal: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
