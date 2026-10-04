import os
import sqlite3
from contextlib import closing

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Write an atomic SQLite snapshot without stopping readers/writers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default=str(settings.STORAGE_PATH / "backups/production.sqlite3"),
        )

    def handle(self, *args, **options):
        from pathlib import Path

        target = Path(options["output"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".pending")
        with (
            closing(sqlite3.connect(settings.DATABASE_PATH)) as source,
            closing(sqlite3.connect(temporary)) as destination,
        ):
            source.backup(destination)
        os.replace(temporary, target)
        queue = settings.STORAGE_PATH / "db/jobs.sqlite3"
        if queue.is_file():
            queue_target = target.with_name("jobs.sqlite3")
            pending = queue_target.with_suffix(".pending")
            with (
                closing(sqlite3.connect(queue)) as source,
                closing(sqlite3.connect(pending)) as destination,
            ):
                source.backup(destination)
            os.replace(pending, queue_target)
        self.stdout.write(str(target))
