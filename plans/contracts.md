# Compatibility and evidence

Runtime behavior is native Django/Python. The original reference is read only and pinned to
`659f 95748a 115a 360a 37db 9bf 80a 5361a 560e 14f`. No sibling implementation is needed at runtime.
Existing schema/storage/password/signed-cookie continuity is the compatibility contract;
byte-for-byte HTML, validators, malformed fragment repair or native-library media output
are not claimed. Raw test and benchmark output remains ignored under `tmp/`.

| Area | Implementation and verification |
|---|---|
| Schema, setup, signup, bcrypt, logout and transfers | Unmanaged original tables; actual first-run browser 4 checks including avatar/default room; independent bidirectional Rails/Django cookie continuity on shared disposable database |
| Room and message authorization | Actual membership required even for open rooms; private reads/search/sidebar/writes and namespace/ownership/deactivation checked with native real-DB tests and independent 18 HTTP checks |
| Messages, paging, search, unread state and boosts | Real persisted writes, RichText embeddings, FTS, attachment filename/mention indexing, created_at paging, refresh 40+40 batches and post-commit Turbo publications |
| Rich text | Native sanitizer, canonical legacy Trix attachments, signed blobs, retained User mentions after key rotation, opengraph and autolinks;64 independently captured canonical-editor plain-text cases |
| Profiles, accounts, invitations, roles and bots | Independent browser 12 admin workflows; role grant/revoke, closed membership revocation, actual bot-key rotation and session revocation |
| Cable | Native Action Cable protocol with shared Redis; independent 10 core browser checks include distinct-user live post/edit/boost/delete; private-stream, forged-signature and signed-out socket 3 security checks |
| Storage and media | Actual libvips image analysis/thumbnail, ffmpeg video preview/audio metadata, PDF first-page preview, signed two-stage direct uploads, ranges and revoked-room download checks |
| Webhooks and push | Native at-least-once leased jobs with fencing/retry/dead state; actual queued local webhook payload and persisted bot reply independently verified; native pywebpush encryption with DNS-pinned public-provider TLS transport |
| Deployment | Native schema preparation and version validation, isolated job SQLite queue, atomic snapshots and checked restore; production Uvicorn multiprocess requires shared Redis; TLS at external proxy |

42 native integration and golden test methods include 19 crypto methods covering independent signed IDs,
app verifiers, encrypted/signed cookies, SGIDs,189 CSRF cases and legacy sessions, plus actual
SQLite workflows/media/rollback/job recovery/backup/restore. No skipped seed-dependent tests.

The wider independent rich-text corpus comparison checked 625 non-ActionText-attachment inputs:
562 exact plain-text matches,63 remaining differences chiefly malformed HTML/fuzz repair,
legacy permissive JSON comments and corpus users absent from the disposable fixture. This
is not full rich-text byte parity. Canonical editor cases have independent expected outputs;
security tests exercise real sanitizer and membership boundaries.

Deliberate direct-ping repair lives in
`assets/overrides/lib/autocomplete/base_autocomplete_handler.js`; reference is unchanged.
Audio attachments retain the original generic download/share presentation; video/PDF/image
previews are real generated media. Push endpoint protection is exercised locally, but live
browser-vendor push delivery requires operator VAPID keys and provider subscriptions.

Queued jobs and app snapshots are separate atomic captures. Restoring may replay side effects;
delivery is at least once. Unsupported older schemas are rejected with required source migration
versions rather than silently modifying an existing installation.
