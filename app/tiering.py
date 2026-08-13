"""
Age-based tiering: move objects out of the hot bucket into cold storage once
they are older than a cutoff.

Run it as a one-shot command (cron, a systemd timer, a compose sidecar):

    python -m app.tiering --dry-run
    python -m app.tiering
    python -m app.tiering --max-age-days 7 --limit 100

This exists because the move cannot be delegated to the storage layer: S3
lifecycle *transitions* target AWS storage classes, and Garage implements
neither storage classes nor lifecycle rules. So it is a copy, a verification,
and only then a delete — in that order, so a crash at any point leaves the
object readable from at least one tier.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import get_cold_storage, get_hot_storage, get_settings
from app.storage.interface import (
    StorageInterface,
    StorageError,
    StorageObjectNotFoundError,
)

logger = logging.getLogger("tiering")


@dataclass
class Stats:
    scanned: int = 0
    eligible: int = 0
    moved: int = 0
    skipped_present: int = 0
    failed: int = 0
    bytes_moved: int = 0

    def summary(self, dry_run: bool) -> str:
        if dry_run:
            return (
                f"{self.scanned} scanned, {self.eligible} eligible, "
                f"0 moved (dry run)"
            )
        return (
            f"{self.scanned} scanned, {self.eligible} eligible, "
            f"{self.moved} moved ({_human_bytes(self.bytes_moved)}), "
            f"{self.skipped_present} already in cold, {self.failed} failed"
        )


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _already_in_cold(cold: StorageInterface, key: str, expected_size: int) -> bool:
    """True only for a byte-complete copy — a truncated one must be redone."""
    try:
        return cold.head_file(key).size == expected_size
    except StorageObjectNotFoundError:
        return False


def move_object(
    hot: StorageInterface,
    cold: StorageInterface,
    key: str,
    expected_size: int,
) -> bool:
    """
    Copy one object hot -> cold, verify the copy, then drop the hot original.
    Returns True if an existing cold copy let it skip the transfer.

    Safe to re-run after a partial failure: a complete cold copy short-circuits
    the transfer, so an interrupted run resumes at the delete.
    """
    reused_copy = _already_in_cold(cold, key, expected_size)

    if reused_copy:
        logger.debug("%s already present in cold storage; skipping copy", key)
    else:
        # Streamed, so object size does not drive memory use.
        with hot.open_file(key) as body:
            cold.upload_file(
                stream=body.stream,
                object_key=key,
                content_type=body.content_type,
            )

        actual_size = cold.head_file(key).size
        if actual_size != expected_size:
            raise StorageError(
                f"Copy of {key} is {actual_size} bytes, expected {expected_size}; "
                "leaving the hot original in place."
            )

    hot.delete_file(key)
    return reused_copy


def run(
    hot: StorageInterface,
    cold: StorageInterface,
    prefix: str,
    max_age_days: int,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> Stats:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    stats = Stats()

    logger.info(
        "Scanning prefix '%s' for objects last modified before %s",
        prefix,
        cutoff.isoformat(timespec="seconds"),
    )

    for info in hot.list_objects(prefix=prefix):
        stats.scanned += 1

        if info.last_modified > cutoff:
            continue

        stats.eligible += 1
        age_days = (datetime.now(timezone.utc) - info.last_modified).days

        if dry_run:
            logger.info("  %s  age=%dd  size=%s  -> would move",
                        info.key, age_days, _human_bytes(info.size))
            continue

        try:
            reused_copy = move_object(hot, cold, info.key, info.size)
        except StorageError as e:
            stats.failed += 1
            logger.error("  %s  FAILED: %s", info.key, e)
            continue

        stats.moved += 1
        stats.bytes_moved += info.size
        if reused_copy:
            stats.skipped_present += 1
        logger.info("  %s  age=%dd  moved (%s)%s", info.key, age_days,
                    _human_bytes(info.size),
                    " [copy already existed]" if reused_copy else "")

        if limit is not None and stats.moved >= limit:
            logger.info("Reached --limit of %d; stopping this pass.", limit)
            break

    return stats


def main(argv: Optional[list] = None) -> int:
    settings = get_settings()

    parser = argparse.ArgumentParser(
        prog="python -m app.tiering",
        description="Move aged-out objects from hot storage to cold (Garage).",
    )
    parser.add_argument(
        "--prefix", default=settings.tiering_prefix,
        help="Key prefix to scan (default: %(default)s)",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=settings.tiering_max_age_days,
        help="Move objects last modified more than this many days ago "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would move without copying or deleting anything.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after moving this many objects.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[tiering] %(message)s",
    )

    if args.max_age_days < 0:
        parser.error("--max-age-days must not be negative")

    cold = get_cold_storage()
    if cold is None:
        logger.error(
            "Cold tier is not configured. Set GARAGE_ACCESS_KEY_ID and "
            "GARAGE_SECRET_ACCESS_KEY in .env (see .env.example)."
        )
        return 2

    stats = run(
        hot=get_hot_storage(),
        cold=cold,
        prefix=args.prefix,
        max_age_days=args.max_age_days,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    logger.info(stats.summary(args.dry_run))
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
