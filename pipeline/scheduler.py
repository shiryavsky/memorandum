"""Scheduler for periodic message ingestion.

This module is kept for non-systemd environments (e.g., macOS development).
For production Linux systems, use the systemd-based approach:
    - bin/memorandum-sync: main sync script with lock protection
    - systemd/memorandum-collect.service + timer: systemd service/timer

If SYSTEMD_MODE env var is set, functions will delegate to systemd instructions.
"""
from .ingest import run_ingest
from config import load_config
import fcntl
import logging
import os
import sys

from datetime import datetime, timedelta, timezone


# Same flock path bin/memorandum-sync uses, so a polling scheduler and a
# manually-triggered sync can't race the SQLite/Chroma state.
_LOCK_FILE = "/tmp/memorandum-sync.lock"


def _acquire_lock(path: str = _LOCK_FILE):
    """Try a non-blocking exclusive flock; return the file handle or None.

    Returning None means another sync/reindex is in flight — caller should
    skip this tick rather than queue up.
    """
    fp = open(path, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        return None
    return fp


def _is_systemd_mode() -> bool:
    """Check if running in systemd environment and not explicitly opting out."""
    if os.getenv("DISABLE_SYSTEMD_CHECK"):
        return False
    return os.path.exists("/run/systemd/system")


def _print_systemd_instructions():
    """Print instructions for systemd-based usage."""
    print("=" * 60)
    print("Scheduler delegation to systemd mode")
    print("=" * 60)
    print()
    print("For Linux systems with systemd, use the bash script instead:")
    print("  ./bin/memorandum-sync [--force]")
    print()
    print("Or use systemd directly:")
    print("  sudo systemctl start memorandum-collect")
    print("  journalctl -u memorandum-collect -f")
    print()
    print("To enable the timer:")
    print("  sudo systemctl enable --now memorandum-collect.timer")
    print()


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def create_scheduler(config_path: str = "config.yaml"):
    """Create and configure the scheduler.

    Args:
        config_path: Path to configuration file

    Returns:
        Simple scheduler function (no blocking)
    """
    config = load_config(config_path)
    schedule_minutes = config.get("schedule_minutes", 15)

    def scheduled_ingest():
        """Job function for scheduled ingestion."""
        lock_fp = _acquire_lock()
        if lock_fp is None:
            logger.warning(
                f"Another sync/reindex holds {_LOCK_FILE}; skipping this tick"
            )
            return
        # Small overlap (2x the interval) to avoid gaps
        since = datetime.now(timezone.utc) - timedelta(minutes=schedule_minutes * 2)
        try:
            stats = run_ingest(since=since, config_path=config_path)
            logger.info(
                f"Scheduled ingest completed: {stats['messages_new']} new, "
                f"{stats['messages_filtered']} filtered, {stats['senders_cached']} senders cached"
            )
        except Exception as e:
            logger.error(f"Scheduled ingest failed: {e}")
        finally:
            lock_fp.close()  # releases flock

    return scheduled_ingest, schedule_minutes


def run_scheduler(config_path: str = "config.yaml"):
    """Run the scheduler using polling.

    This function blocks and runs indefinitely. Handle SIGTERM/SIGINT for graceful shutdown.
    NOTE: This is a fallback for non-systemd environments.

    Args:
        config_path: Path to configuration file
    """
    # Check for systemd environment - if found, show instructions and exit
    if _is_systemd_mode():
        _print_systemd_instructions()
        sys.exit(0)

    import signal
    import time

    logger.warning("=" * 60)
    logger.warning("USING FALLBACK SCHEDULER (polling)")
    logger.warning("For production, use systemd: ./bin/memorandum-sync")
    logger.warning("=" * 60)

    job_func, schedule_minutes = create_scheduler(config_path)
    interval_seconds = schedule_minutes * 60

    running = True

    def shutdown_handler(signum, frame):
        nonlocal running
        logger.info("Shutdown signal received, stopping scheduler...")
        running = False

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    logger.info(f"Starting message collector scheduler (every {schedule_minutes} minutes)...")
    logger.info("Press Ctrl+C to stop")

    # Run immediately on start
    job_func()

    while running:
        try:
            time.sleep(interval_seconds)
            if running:
                job_func()
        except KeyboardInterrupt:
            break

    logger.info("Scheduler stopped")


def main():
    """CLI entry point for running the scheduler."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Run message collector scheduler",
        epilog="For production use on Linux, prefer systemd: ./bin/memorandum-sync"
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config file"
    )
    parser.add_argument(
        "--systemd", action="store_true",
        help="Show systemd instructions and exit"
    )
    args = parser.parse_args()

    if args.systemd:
        _print_systemd_instructions()
        sys.exit(0)
    run_scheduler(config_path=args.config)


if __name__ == "__main__":
    main()
