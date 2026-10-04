"""Restore ONCE snapshots while the application is stopped."""

import os
import shutil
import sqlite3
from contextlib import closing

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    requires_system_checks = []
    help = "Restore checked application and queued-job snapshots before startup."

    def handle(self, *args, **options):
        snapshots = settings.STORAGE_PATH / "backups"
        for filename in ("production.sqlite3", "jobs.sqlite3"):
            source = snapshots / filename
            if not source.is_file():
                if filename == "production.sqlite3":
                    raise CommandError("Application snapshot is missing")
                continue
            with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise CommandError(f"Invalid snapshot: {filename}")
            target = (
                settings.DATABASE_PATH
                if filename == "production.sqlite3"
                else settings.STORAGE_PATH / "db/jobs.sqlite3"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".restoring")
            shutil.copyfile(source, temporary)
            for suffix in ("-wal", "-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            os.replace(temporary, target)
        self.stdout.write("Snapshots restored")
