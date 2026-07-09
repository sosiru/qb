import logging
import os
import sys
import threading
import time

from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections

logger = logging.getLogger(__name__)

COMMANDS = (
    "send_reminders",
    "run_due_payments",
    "process_outbox",
    "process_notifications",
)
INTERVAL_SECONDS = 60

_started = False
_lock = threading.Lock()


def should_start_scheduler():
    if os.environ.get("QB_BACKGROUND_COMMANDS_ENABLED", "1") != "1":
        return False
    if "test" in sys.argv or "migrate" in sys.argv or "makemigrations" in sys.argv:
        return False
    if not getattr(settings, "BACKGROUND_COMMANDS_ENABLED", True):
        return False
    if "runserver" in sys.argv:
        return os.environ.get("RUN_MAIN") == "true"
    return os.environ.get("QB_FORCE_BACKGROUND_COMMANDS", "0") == "1"


def start_background_commands():
    global _started
    if not should_start_scheduler():
        return
    with _lock:
        if _started:
            return
        thread = threading.Thread(target=_run_forever, name="qb-background-commands", daemon=True)
        thread.start()
        _started = True
        logger.info("background.commands.started interval_seconds=%s commands=%s", INTERVAL_SECONDS, ",".join(COMMANDS))


def _run_forever():
    while True:
        started_at = time.monotonic()
        for command_name in COMMANDS:
            try:
                close_old_connections()
                call_command(command_name, verbosity=0)
                logger.info("background.command.success command=%s", command_name)
            except Exception:
                logger.exception("background.command.failed command=%s", command_name)
            finally:
                close_old_connections()
        elapsed = time.monotonic() - started_at
        time.sleep(max(INTERVAL_SECONDS - elapsed, 1))
