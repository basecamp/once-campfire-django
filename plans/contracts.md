# Compatibility and evidence

Runtime behavior is native Django/Python. The original reference is read only and pinned to
`659f95748a115a360a37db9bf80a5361a560e14f`. No sibling implementation is needed at runtime.
Existing schema/storage/password/signed-cookie continuity is the compatibility contract;
byte-for-byte HTML, validators, malformed fragment repair or native-library media output
are not claimed. Raw test and benchmark output remains ignored under `tmp/`.

| Area | Implementation and verification |
|---|---|
| Schema, setup, signup, bcrypt, logout and transfers | Unmanaged original tables; actual first-run browser 4 checks including avatar/default room; independent bidirectional Rails/Django cookie continuity on shared disposable database |
| Room and message authorization | Actual membership required even for open rooms; private reads/search/sidebar/writes and namespace/ownership/deactivation checked with native real-DB tests and independent 18 HTTP checks |
| Messages, paging, search, unread state and boosts | Real persisted writes and atomic signed/multipart attachment updates, RichText embeddings, FTS, attachment filename/mention indexing, created_at paging, refresh 40+40 batches and post-commit Turbo publications |
| Rich text | Native sanitizer, canonical legacy Trix attachments, signed blobs, retained User mentions after key rotation, opengraph and autolinks;64 independently captured canonical-editor plain-text cases |
| Profiles, accounts, invitations, roles and bots | Independent browser 12 admin workflows; role grant/revoke, closed membership revocation, actual bot-key rotation and session revocation |
| Cable | Native Action Cable protocol with shared Redis; independent 10 core browser checks include distinct-user live post/edit/boost/delete; private-stream, forged-signature and signed-out socket 3 security checks |
| Storage and media | Actual libvips image analysis/thumbnail, ffmpeg video preview/audio metadata, PDF first-page preview, signed two-stage direct uploads with draft owner/legacy URL access, ranges, forced-binary active-content downloads and revoked-room download checks |
| Webhooks and push | Native at-least-once leased jobs with fencing/retry/dead state; actual queued local webhook payload and persisted bot reply independently verified; generated replies suppress recursive webhook delivery while retaining push; native pywebpush encryption with DNS-pinned public-provider TLS transport |
| Benchmarks | Independent matched production HTTP runs verify identical ordered message/search windows, successful acknowledged writes, FTS and SQLite integrity. Actual JPEG uploads resize to 1200×675. Two paced runs admit 100 sockets and deliver all 30 messages to every socket; these are measured workloads, not capacity limits. Raw output stays ignored. |
| Deployment | Native schema preparation and version validation, isolated job SQLite queue, atomic snapshots and checked restore; production Uvicorn multiprocess requires shared Redis; TLS at external proxy |

73 native integration and golden test methods include 19 crypto methods covering independent signed IDs,
app verifiers, encrypted/signed cookies, SGIDs,189 historical CSRF crypto vectors and legacy sessions, plus actual
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

Browser request forgery protection deliberately uses the Rust Fetch Metadata policy, not the
historical Rails token protocol. Native tests cover the complete method/origin/metadata/TLS
matrix, HTTPS token-free login, old cookie continuity, token-free setup/transfer and signed
owner-authorized uploads. Complete identity/gzip cache bytes preserve literal token-like user
text, with fresh authorization and foreign-commit invalidation. Uvicorn's configured trusted
proxy policy supplies the effective scheme; forwarded headers are not trusted in Django itself.

Message creation prepares only pure sanitization and attachment-free plain text before acquiring
SQLite's writer lock. Membership and signed attachment/mention records remain current inside
the transaction; message, rich text, FTS, room recency and unread markers commit together.
Fresh creation skips nonexistent FTS deletes and attachment/boost reloads, then renders one
post-commit fragment reused for both Cable and the HTTP response. Native controls cover
membership revocation and mention renaming immediately before BEGIN, unread-trigger rollback,
shared first-unread versus direct latest-unread markers, and exact Cable/HTTP fragment equality.

Notifications retain individual leased jobs but insert one message's jobs in one atomic queue
transaction. Cable retains each ordered publication and sends them in one Redis pipeline.
Native controls check payload order/count, one queue commit and whole-batch rollback.
