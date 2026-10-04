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

Measured with 16 concurrent clients on an AMD Ryzen AI MAX+ 395,
with four hardware threads allocated to each app.

| Requests/second | Rails | Django | Laravel |
|---|---:|---:|---:|
| Room | 242 | 170 | 164 |
| Messages | 402 | 196 | 175 |
| Sidebar | 541 | 615 | 715 |
| Search | 424 | 315 | 305 |
| Post message | 225 | 154 | 137 |

At 100 WebSocket connections and five messages/second, median delivery to every
connection was 24 ms for Rails, 70 ms for Django and 42 ms for Laravel. Every message
reached every connection in both runs.

## Known differences

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
