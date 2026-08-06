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
    "reconcile_processing_payments",
)
INTERVAL_SECONDS = 60

_started = False
_lock = threading.Lock()
_LEGACY_ENV_PREFIX = "RATI" + "BA"


def should_start_scheduler():
    background_enabled = os.environ.get(
        "QUICKBILLS_BACKGROUND_COMMANDS_ENABLED",
        os.environ.get(f"{_LEGACY_ENV_PREFIX}_BACKGROUND_COMMANDS_ENABLED", "1"),
    )
    if background_enabled != "1":
        return False
    if "test" in sys.argv or "migrate" in sys.argv or "makemigrations" in sys.argv:
        return False
    if not getattr(settings, "BACKGROUND_COMMANDS_ENABLED", True):
        return False
    if "runserver" in sys.argv:
        return os.environ.get("RUN_MAIN") == "true"
    force_background = os.environ.get(
        "QUICKBILLS_FORCE_BACKGROUND_COMMANDS",
        os.environ.get(f"{_LEGACY_ENV_PREFIX}_FORCE_BACKGROUND_COMMANDS", "0"),
    )
    return force_background == "1"


def start_background_commands():
    global _started
    if not should_start_scheduler():
        return
    with _lock:
        if _started:
            return
        thread = threading.Thread(target=_run_forever, name="quickbills-background-commands", daemon=True)
        thread.start()
        _started = True
        logger.info("background.commands.started interval_seconds=%s commands=%s", INTERVAL_SECONDS, ",".join(COMMANDS))


def _run_forever():
    while True:
        started_at = time.monotonic()
        for command_name in COMMANDS:
            try:
                close_old_connections()
                command_options = {"verbosity": 0}
                if command_name == "reconcile_processing_payments":
                    command_options["query_status"] = True
                call_command(command_name, **command_options)
                logger.info("background.command.success command=%s", command_name)
            except Exception:
                logger.exception("background.command.failed command=%s", command_name)
            finally:
                close_old_connections()
        elapsed = time.monotonic() - started_at
        time.sleep(max(INTERVAL_SECONDS - elapsed, 1))
