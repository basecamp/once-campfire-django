import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("SECRET_KEY_BASE", "")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY_BASE is required")
DEBUG = os.environ.get("CAMPFIRE_DEBUG") == "1"
FORCE_SSL = os.environ.get("FORCE_SSL", "").lower() in ("1", "true")
RESPONSE_CACHE_BYTES = (
    max(0, int(os.environ.get("CAMPFIRE_RESPONSE_CACHE_MB", "64"))) * 1024 * 1024
)
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")
ROOT_URLCONF = "campfire.urls"
INSTALLED_APPS = ["campfire"]
MIDDLEWARE = [
    "campfire.compression.CompressionMiddleware",
    "campfire.middleware.SessionMiddleware",
    "campfire.middleware.SecurityMiddleware",
]
STORAGE_PATH = Path(os.environ.get("CAMPFIRE_STORAGE_PATH", BASE_DIR / "storage"))
FILES_PATH = Path(os.environ.get("CAMPFIRE_FILES_PATH", STORAGE_PATH / "files"))
DATABASE_PATH = Path(
    os.environ.get("CAMPFIRE_DATABASE_PATH", STORAGE_PATH / "db" / "production.sqlite3")
)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATABASE_PATH,
        "OPTIONS": {
            "timeout": 10,
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
        "CONN_MAX_AGE": 0,
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = True
TIME_ZONE = "UTC"
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
