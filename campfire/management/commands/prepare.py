import sqlite3

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    requires_system_checks = []
    help = (
        "Initialize an empty Rails-compatible schema; reject unsupported upgrade state."
    )

    def handle(self, *args, **options):
        settings.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        settings.FILES_PATH.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(settings.DATABASE_PATH)
        if not db.execute(
            "SELECT name FROM sqlite_master WHERE name='accounts'"
        ).fetchone():
            db.executescript((settings.BASE_DIR / "campfire/schema.sql").read_text())
            versions = [
                p.stem.split("_", 1)[0]
                for p in (settings.BASE_DIR / "reference/db/migrate").glob("*.rb")
            ]
            db.executemany(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                [(v,) for v in versions],
            )
            db.commit()
        else:
            required = [
                p.stem.split("_", 1)[0]
                for p in (settings.BASE_DIR / "reference/db/migrate").glob("*.rb")
            ]
            present = {
                r[0] for r in db.execute("SELECT version FROM schema_migrations")
            }
            missing = set(required) - present
            if missing:
                raise CommandError(
                    "Run the pinned Rails database migrations before upgrade: "
                    + ", ".join(sorted(missing))
                )
        db.execute("PRAGMA journal_mode=WAL")
        db.close()
        self.stdout.write("Database ready")
