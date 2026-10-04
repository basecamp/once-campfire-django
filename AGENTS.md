# once-campfire-django

Native Django Campfire. Read the pinned public Rails `reference/` for existing behavior;
never change its checkout. Runtime must remain self-contained Python/Django.

- Preserve the existing SQLite schema, storage keys, bcrypt passwords and Rails cookies.
  Models are unmanaged; framework authentication/session tables are not used.
- Native jobs use `storage/db/jobs.sqlite3` with leased, fenced, bounded retries. Redis
  shares Cable publications and rate limits across workers; it is required when
  `WEB_WORKERS` exceeds one. Per-publication session/membership checks prevent stale access.
- Keep deliberately changed behavior in README's "Known differences" and update
  `plans/contracts.md` with actual evidence and outstanding limits.
- Frontend changes belong in `assets/overrides/`; run `python bin/build-assets` afterward.
  Vendored framework asset distributions retain their licenses and upstream formatting.
- Install pinned requirements in `.venv`. Verify with
  `SECRET_KEY_BASE=tests-only PATH="$PWD/.venv/bin:$PATH" bin/check`.
  Tests create isolated real SQLite databases under ignored `tmp/`; no seeds are skipped.
- Format/check first-party Python with Ruff; never reformat vendored frontend assets.
- Build production with `docker build --build-arg REVISION="$(git rev-parse HEAD)" -t once-campfire-django:benchmark .`.
  `HTTP_PORT` configures the listener, `CAMPFIRE_STORAGE_PATH` the mounted storage root,
  `SECRET_KEY_BASE` the retained installation key, and `TRUSTED_PROXIES` the TLS proxy IP.
- Back up with `python manage.py backup`; restore with `python manage.py restore` while
  the app is stopped. Application and queue backups are separate atomic captures;
  external job side effects have at-least-once delivery.
- Keep raw benchmark/test results, scratch generators and runtime storage ignored.
  Do not commit result JSON files. Independent expected-output vectors are test fixtures.
