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
SQLite commit invalidates complete cached HTML and gzip bodies.
Jobs use a leased SQLite queue that survives restarts. Put TLS termination in front of the application and configure
`TRUSTED_PROXIES` to that proxy's address.

Native integration and Rails golden tests cover real media,
attachment updates and queued bot replies. Run `PATH="$PWD/.venv/bin:$PATH" bin/check`.

## Benchmarks

Measured with 16 concurrent clients on an AMD Ryzen AI MAX+ 395 with 32 GB RAM,
with four hardware cores allocated to each app.

| HTTP workload (requests/sec) | Rails | [Django](https://github.com/basecamp/once-campfire-django) | [Laravel](https://github.com/basecamp/once-campfire-laravel) | [Express](https://github.com/basecamp/once-campfire-express) | [Elixir](https://github.com/basecamp/once-campfire-elixir) | [Go](https://github.com/basecamp/once-campfire-go) | [Rust](https://github.com/basecamp/once-campfire-rust) | [C](https://github.com/basecamp/once-campfire-c) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Room page | 2,063 | 478 | 3,038 | 43,925 | 5,350 | 53,060 | 106,494 | 137,524 |
| Messages page | 2,063 | 486 | 3,081 | 74,176 | 5,712 | 54,800 | 102,697 | 144,642 |
| Sidebar | 2,545 | 601 | 3,832 | 94,322 | 5,949 | 59,144 | 120,294 | 152,002 |
| Search | 2,528 | 594 | 3,710 | 82,937 | 5,848 | 60,509 | 121,378 | 149,487 |
| Post a message | 234 | 112 | 577 | 2,098 | 1,278 | 9,021 | 8,037 | 7,530 |

[Shared verification](https://github.com/basecamp/once-campfire-verification) · [Detailed results](https://github.com/basecamp/once-campfire-verification/blob/main/docs/performance-review.md).


## Known differences

- Browser writes use Fetch Metadata instead of CSRF tokens. Unsafe requests reject a mismatched
  or null Origin and require `Sec-Fetch-Site: same-origin` or `same-site`; missing metadata is
  accepted only over plain HTTP without `FORCE_SSL=1` or `true`. GET/HEAD and authenticated bot routes
  retain their existing behavior. Forms and uploads generate no CSRF tokens; old signed cookies
  and token-bearing tabs continue to work. Signed disk uploads require the authenticated owner
  and the expiring upload capability, independently of Fetch Metadata.

- Authenticated read pages use a bounded, commit-versioned cache after fresh authorization;
  complete HTML and gzip representations are reused while cookies remain per request.

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
