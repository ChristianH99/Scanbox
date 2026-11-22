# Destinations

All destinations implement the protocol in `backend/scanbox/destinations/base.py`
(see ARCHITECTURE.md §10). User-editable templates (subject, body, Paperless title) are
rendered with `scanbox.destinations.templating.render_template`. It replaces only `{name}`
tokens that are known variables (`title`, `filename`, `qr`, `date`, `time`, `pages`, `profile`,
`user`). Unknown tokens stay as written. `{{` and `}}` produce literal braces. Attribute access,
indexing and format specs are never evaluated, so it is safe with user input.

## Email (`type = "email"`)

Module `scanbox.destinations.email`: `EmailDestination`, `EmailConfig`, `test_smtp(env)`,
`send_test_mail(env, to)`.

### Admin settings

| Key | Default | Notes |
|---|---|---|
| `smtp.host` | – | required |
| `smtp.port` | 587 (465 for `tls`) | |
| `smtp.security` | `starttls` | `starttls` (STARTTLS required), `tls` (implicit TLS), `none` |
| `smtp.username` / `smtp.password` | – | AUTH is used only when a username is set |
| `smtp.from_address` | – | required; also the Message-ID domain |
| `smtp.from_name` | – | display name |
| `smtp.max_attachment_mb` | 25 | limit for the **whole encoded message**, in decimal MB |
| `smtp.timeout_s` | 30 | connect/command timeout |
| `smtp.verify_tls` | true | *new key*: set false only for self-signed servers on the LAN |

### User settings

`email.default_recipients` (list), `email.subject_template`, `email.body_template`.

### Profile config (`EmailConfig`)

| Field | Default | |
|---|---|---|
| `recipients` | `[]` | validated addresses |
| `use_default_recipients` | true | merge the user's default recipients (deduplicated, case-insensitive) |
| `subject_template` | null | null → user setting → `Scan: {title}` |
| `body_template` | null | null → user setting → built-in text |
| `attach_text` | false | attaches the OCR `.txt` artifact (or `job.ocr_text`) |
| `oversize` | `compress_then_split` | also `compress`, `split`, `fail` |

If no recipients result at all, the mail goes to the user's own address (`job.user_email`).
If that is also missing, the delivery fails with `no_recipients`.

### Size handling

Limits such as Gmail's 25 MB apply to the encoded message. Base64 adds about 37 %
(4/3 plus line breaks), so Scanbox builds the real MIME message and measures it. It also
keeps 64 KB of headroom for headers that relays add. If the message is too large:

1. `compress` / `compress_then_split`: `job.recompress("ebook")` (Ghostscript), then measure again.
2. `split` / `compress_then_split`: the PDF is split by page ranges (pikepdf) into parts that
   fit. Each part is sent as its own mail with `[i/n]` appended to the subject. The parts are
   named `<name>_part<i>of<n>.pdf`. The OCR text is attached to part 1 only.
3. Otherwise, or if a single page alone is too large, the delivery fails with
   `attachment_too_large` (not retryable).

### Idempotency and recovery

* `Message-ID` is `<scanbox.<sha256(idempotency_key)[:32]>.<i>of<n>@<from-domain>>`. A retry of
  the same delivery run produces the same IDs, so recipients (and many mail servers) can spot
  duplicates.
* **Resumable multi-part sending.** If the runner passes `env.extras["progress"]` (a sync or
  async callable), `deliver` reports `{"plan", "parts", "sent_parts", "in_flight", "message_ids",
  "recipients"}` before and after each mail. The runner should store this as the delivery's
  partial result. It passes it back as `env.extras["previous_result"]` on retries and as
  `previous` to `recover`. `deliver` skips parts already in `sent_parts` when the plan (source
  sha256 + part count) is unchanged.
* `recover(previous)`:
  * no progress information → `NEEDS_ATTENTION` (the mail may have been sent);
  * `in_flight` set (crash during a send) → `NEEDS_ATTENTION`;
  * all parts sent → `DONE`;
  * otherwise → `REDELIVER` (the remaining parts are sent).
* After a definite SMTP rejection, `in_flight` is cleared. After a dropped connection it stays
  set, because the server may have accepted the mail.

### Error classification

| Situation | code | retryable |
|---|---|---|
| connection refused, timeout, disconnect | `smtp_unavailable` | yes |
| 4xx reply (e.g. 451, 421) | `smtp_error` | yes |
| 5xx reply | `smtp_error` | no |
| 552/523 size rejected | `attachment_too_large` | no |
| 535 auth failure (454 temporary auth failure is retryable) | `smtp_auth_failed` | no |
| all recipients refused with 5xx | `recipients_refused` | no |
| TLS certificate invalid | `smtp_tls_error` | no |
| host/from missing | `smtp_not_configured` | no |

If only some recipients are refused, the delivery succeeds and the refused addresses are
listed in `detail.refused`.

### Admin helpers

* `test_smtp(env) -> (ok, message)` connects, negotiates TLS and logs in. It sends no mail.
* `send_test_mail(env, to)` sends a short text mail and raises `DeliveryError` on failure.

## Paperless-ngx (`type = "paperless"`)

Module `scanbox.destinations.paperless`: `PaperlessDestination`, `PaperlessConfig`,
`test_paperless(env)`.

### Settings

Admin: `paperless.url`, `paperless.consume_dir`, `paperless.verify_tls` (true).
User: `paperless.token` (create it in Paperless under *Profile → API Auth Token*).

### Profile config (`PaperlessConfig`)

| Field | Default | |
|---|---|---|
| `mode` | `api` | `api` or `consume` |
| `title_template` | `{title}` | API mode only |
| `tags` | `[]` | tag **names**, resolved case-insensitively |
| `create_missing_tags` | false | otherwise an unknown tag fails (not retryable) |
| `correspondent`, `document_type` | null | names; must exist |
| `set_created_date` | true | sends the scan time as `created` |
| `qr_mapping` | `asn` | `none`, `asn`, `custom_field`, `tag` |
| `qr_custom_field` | null | custom field name (needed for `custom_field` mapping/fallback) |
| `asn_fallback` | `none` | used when the QR value is not an integer between 0 and 4294967295: `custom_field`, `tag`, `none` |
| `skip_ocr` | false | send `job.image_only_pdf` (if available), so Paperless runs its own OCR |
| `wait_for_task` | true | poll the consumption task (≤ 60 s) to report failures |
| `consume_subdir` | null | consume mode only; sanitised per segment, `..` and absolute paths are rejected |

QR tags (`qr_mapping=tag` or `asn_fallback=tag`) are always created when missing, because
they are per-document values. For an `integer` custom field, the QR value must be numeric.

### API mode

Targets Paperless-ngx 2.x/3.x. The client sends `Accept: application/json; version=9`
(supported by current releases). If the server answers 406 (it is older), the client retries
without a version and uses the server default.

* `POST /api/documents/post_document/` (multipart) with `document`, `title`, `created`
  (ISO datetime; API ≥ 9 stores the date only), `correspondent`, `document_type`, `tags`
  (repeated ids), `archive_serial_number`, and `custom_fields` as a JSON object
  `{"<field id>": value}`. If the server rejects the object form (old 2.x accepted only a list
  of ids), Scanbox uploads without it. Once the task reports the document id, it sets the value
  with `PATCH /api/documents/<id>/`.
* The response is the task UUID. With `wait_for_task`, Scanbox polls `GET /api/tasks/?task_id=`
  (a list in API ≤ 9, paginated in API 10):
  * `SUCCESS` → the result stores `document_id` (from `related_document`).
  * `FAILURE` with "It is a duplicate of … (#id)" → success, `duplicate: true`.
  * Other failures (e.g. "Given ASN … already exists!") → `paperless_consume_failed`, not retryable.
  * Still pending after 60 s → success with a warning, because the document is queued in Paperless.
* Result detail: `task_id`, `task_status`, `document_id`, `sha256`, `asn` / `asn_fallback` /
  `custom_field` / `qr_tag`, `paperless_version` (from the `X-Version` header).

**Duplicates.** Paperless-ngx < 3 rejects files whose checksum already exists. Since 3.0,
Paperless *accepts* duplicates by default (`PAPERLESS_CONSUMER_DELETE_DUPLICATES=false`). So
Scanbox does not rely on the rejection. On a retry (`job.attempt > 1`) and in `recover`, it
first looks for the document with `GET /api/documents/?checksum__iexact=`. It checks both
SHA-256 (3.x) and MD5 (2.x).

Errors: connection errors, timeouts, 5xx and 429 → retryable. 401/403 →
`paperless_auth_failed` (invalid token), not retryable. Other 4xx → `paperless_rejected` with
Paperless's field messages, not retryable. A missing URL or token → `paperless_not_configured`,
not retryable.

`recover`: a stored `task_id` that is SUCCESS/PENDING/duplicate → `DONE`. A document with the
same checksum → `DONE`. Otherwise (including Paperless unreachable) → `REDELIVER`.

`test_paperless(env)` calls `GET /api/tags/?page_size=1` with the token and reports
"Connected to Paperless-ngx <X-Version> (API v<X-Api-Version>)".

### Consume mode

The file is written to `paperless.consume_dir[/consume_subdir]`:

* It is first written to a dot-prefixed temp file (`.scanbox-<random>.tmp`, ignored by the
  Paperless consumer) in the same directory, then fsync'd. The final name is then created
  atomically with `os.link`, which fails if the name exists, so a file is never overwritten.
  On a conflict the name becomes `name (2).pdf`, `name (3).pdf`, … On filesystems without hard
  links, Scanbox falls back to an existence check + `os.replace`.
* The resolved target must stay inside the consume dir. Symlink escapes and `..` are rejected.
* A missing consume dir is retryable (e.g. an unmounted share). Configuration errors are not.
* `recover`: `DONE` if a non-hidden file with the same SHA-256 is still in the target dir,
  otherwise `REDELIVER`. Paperless deletes consumed files, so after consumption a re-delivery
  can create a duplicate on Paperless ≥ 3 (unless `PAPERLESS_CONSUMER_DELETE_DUPLICATES=true`).

**Not possible in consume mode:** title (Paperless derives it from the filename), created date
(it is parsed from the content or filename), tags (except through
`PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS=true` + `consume_subdir`), correspondent, document type,
ASN (except through Paperless's own ASN barcode detection, `PAPERLESS_CONSUMER_ENABLE_ASN_BARCODE`),
custom fields, and failure reporting. Use Paperless workflows (trigger "Consume folder", filter
on path) for the rest.

### Development mock

`backend/tests/mocks/paperless.py` is an in-process mock of the API used by the tests. To run it
standalone, go to `backend/` and run `python -m tests.mocks.paperless --port 8000 --token dev-token`
(`--api-version`, `--allow-duplicates`). Toggles on `app.state.mock`: `offline`, `fail_status`,
`delay_s`, `reject_duplicates`, `legacy_custom_fields`, `consume_error`.
