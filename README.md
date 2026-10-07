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

A single process works without Redis. Multiple HTTP workers require `REDIS_URL` for shared
Cable publications and rate limits. Jobs use a leased SQLite queue that survives restarts. Put TLS termination in front of the application and configure
`TRUSTED_PROXIES` to that proxy's address.

46 native integration and Rails golden test methods pass, including real media,
attachment updates and queued bot replies. Run `PATH="$PWD/.venv/bin:$PATH" bin/check`.

## Benchmarks

Measured with 16 concurrent clients on an AMD Ryzen AI MAX+ 395 with 32 GB RAM,
with four hardware threads allocated to each app.

| HTTP workload (requests/sec) | Rails | [Django](https://github.com/basecamp/once-campfire-django) | [Laravel](https://github.com/basecamp/once-campfire-laravel) | [Express](https://github.com/basecamp/once-campfire-express) | [Elixir](https://github.com/basecamp/once-campfire-elixir) | [Go](https://github.com/basecamp/once-campfire-go) | [Rust](https://github.com/basecamp/once-campfire-rust) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Room page | 236 | 62 | 764 | 2,702 | 981 | 32,132 | 35,056 |
| Messages page | 384 | 70 | 922 | 3,183 | 1,341 | 31,564 | 40,481 |
| Sidebar | 474 | 230 | 1,399 | 34,595 | 2,546 | 17,993 | 33,924 |
| Search | 415 | 120 | 1,291 | 6,725 | 1,907 | 29,775 | 34,199 |
| Post a message | 244 | 113 | 498 | 2,183 | 1,431 | 9,442 | 8,995 |


## Known differences

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
