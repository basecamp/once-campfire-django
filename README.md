# once-campfire-django

A native Django implementation of ONCE Campfire. Django models map the existing SQLite
schema, Jinja templates retain the original Turbo/Stimulus/Lexxy interface, Uvicorn serves
HTTP and Action Cable WebSockets, and libvips/ffmpeg process media. No Ruby or another
Campfire implementation runs in the application process.

The original schema, files, bcrypt passwords and Rails sessions remain compatible.
See [contracts](plans/contracts.md) for verification and remaining compatibility limits.

```sh
git submodule update --init
python -m venv .venv
.venv/bin/pip install -r requirements.txt
bin/build-assets
export SECRET_KEY_BASE="$(openssl rand -hex 64)"
PATH="$PWD/.venv/bin:$PATH" HTTP_PORT=8080 bin/server
```

Storage defaults to `storage/db/production.sqlite3` and `storage/files`. Keep the existing
`SECRET_KEY_BASE` to retain signed/encrypted cookies. Set `CAMPFIRE_STORAGE_PATH` to relocate
both. `python manage.py backup` snapshots the application and job queue;
`python manage.py restore` restores snapshots while the application is stopped.

A single process works without Redis. With `REDIS_URL`, HTTP workers default to the assigned
CPU count, capped at four; `WEB_WORKERS` overrides it. Multiple workers require Redis for shared
Cable publications and rate limits. Each worker has a 64 MiB page-cache budget;
`CAMPFIRE_RESPONSE_CACHE_MB=0` disables it. Authentication and room access stay fresh, every
SQLite commit invalidates cached pages, and CSRF masks and gzip padding remain per request.
Jobs use a leased SQLite queue that survives restarts. Put TLS termination in front of the application and configure
`TRUSTED_PROXIES` to that proxy's address.

Native integration and Rails golden tests cover real media,
attachment updates and queued bot replies. Run `PATH="$PWD/.venv/bin:$PATH" bin/check`.

## Benchmarks

Measured with 16 concurrent clients on an AMD Ryzen AI MAX+ 395 with 32 GB RAM,
with four hardware cores allocated to each app.

| HTTP workload (requests/sec) | Rails | [Django](https://github.com/basecamp/once-campfire-django) | [Laravel](https://github.com/basecamp/once-campfire-laravel) | [Express](https://github.com/basecamp/once-campfire-express) | [Elixir](https://github.com/basecamp/once-campfire-elixir) | [Go](https://github.com/basecamp/once-campfire-go) | [Rust](https://github.com/basecamp/once-campfire-rust) | [C](https://github.com/basecamp/once-campfire-c) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Room page | 230 | 62 | 760 | 2,622 | 942 | 31,673 | 35,484 | 141,834 |
| Messages page | 402 | 70 | 924 | 3,245 | 1,267 | 30,746 | 40,674 | 151,564 |
| Sidebar | 468 | 229 | 1,383 | 34,938 | 2,515 | 18,586 | 34,479 | 159,850 |
| Search | 399 | 118 | 1,135 | 6,613 | 1,814 | 29,765 | 34,432 | 155,456 |
| Post a message | 248 | 112 | 498 | 2,088 | 1,400 | 9,073 | 8,998 | 7,460 |

[Shared verification](https://github.com/basecamp/once-campfire-verification) · [Detailed results](https://github.com/basecamp/once-campfire-verification/blob/main/docs/performance-review.md).


## Known differences

- Authenticated read pages use a bounded, commit-versioned cache after fresh authorization;
  CSRF masks and response compression remain per request.

- Sidebar connection refresh waits for the current Turbo frame to finish loading,
  preventing an aborted response on startup or reconnect. Obsolete connections and removed frames do not reload.

- Cached message copy-link buttons store paths and resolve them against the current page,
  keeping copied links absolute without embedding a request host in shared markup.

- Search selects the newest 100 matching messages by insertion ID, then displays them in ID order. Backdated messages can appear in a different order from the original Rails app.

- TLS terminates at a configured proxy.
- Attached message downloads recheck room membership; native draft uploads belong to their
  uploader. Existing Rails unattached signed draft URLs remain usable after sign-in.
- Direct-ping autocomplete explicitly requests JSON, repairing the original fetch-header bug.
- HTML whitespace, malformed HTML repair, cache validators and native-library media bytes
  can differ; canonical editor plain text and actual browser workflows are tested.
- App and queue snapshots are separate atomic SQLite backups; job delivery is at least once.

MIT; templates and asset-generation algorithms were adapted from the Go port, and media and
Rails compatibility contracts from the original application and existing ports. The immutable
original reference is pinned to `659f95748a115a360a37db9bf80a5361a560e14f`.
