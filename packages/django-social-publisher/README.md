# django-social-publisher

A reusable Django app that connects users' social accounts through official OAuth and publishes
video through official platform APIs. It tracks what each account can do, makes uploads
idempotent, and reports only what the platform confirmed.

Version 0.1 ships a YouTube adapter. The adapter interface lets you add your own platforms.

Supports Python 3.11 to 3.14 and Django 5.2, 6.0 and 6.1. Celery and DRF are optional.

## Five-minute YouTube quickstart

1. Install the package:

   ```bash
   pip install "django-social-publisher[celery,drf]"
   ```

2. In Google Cloud, enable **YouTube Data API v3** and create an OAuth client of type "Web
   application". Add your redirect URI.

3. Generate an encryption key and keep it outside the database:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

4. Configure Django:

   ```python
   INSTALLED_APPS = [..., "django.contrib.contenttypes", "social_publisher"]

   SOCIAL_PUBLISHER = {
       "TOKEN_ENCRYPTION_KEYS": [os.environ["SOCIAL_TOKEN_KEY"]],
       "MEDIA_OPENER": "myproject.media.open_media",
       "PLATFORMS": {
           "youtube": {
               "CLIENT_ID": os.environ["YOUTUBE_CLIENT_ID"],
               "CLIENT_SECRET": os.environ["YOUTUBE_CLIENT_SECRET"],
               "REDIRECT_URI": "https://app.example/oauth/youtube",
           },
       },
   }
   ```

5. Supply the opener. The package never touches storage itself. Return a readable, seekable
   binary stream from any backend, because resuming an upload needs `seek`:

   ```python
   from django.core.files.storage import default_storage


   def open_media(ref):
       return default_storage.open(ref, "rb")
   ```

6. Run `python manage.py migrate`, then connect an account and publish:

   ```python
   from social_publisher import services
   from social_publisher.errors import OutcomeUnknown
   from social_publisher.types import MediaSpec, PublishMetadata

   url = services.start_connection(request.user, "youtube")  # redirect the user here
   account = services.complete_connection(state, code, owner=request.user)

   media = MediaSpec(
       ref="videos/a.mp4",
       mime_type="video/mp4",
       size_bytes=size,
       duration_seconds=42.0,
       width=1080,
       height=1920,
       kind="short",
   )
   meta = PublishMetadata(
       title="My Short", visibility="private", made_for_kids=False, contains_synthetic_media=True
   )
   try:
       publication = services.publish(account, media, meta, idempotency_key="video-123")
   except OutcomeUnknown as exc:
       publication = exc.publication  # reconcile; never upload again

   publication = services.reconcile(publication)  # call until published or failed
   ```

`publish` returns a publication in `processing`. It becomes `published` only after a later
`reconcile` sees the video on the platform with its processing finished.

## YouTube audit and quota limits

These facts come from Google's documentation as checked on 4 Nov 2026. They change often, so check
the [revision history](https://developers.google.com/youtube/v3/revision_history).

**Uploads from unaudited projects.** From 28 Jul 2020 to 2 Nov 2026, YouTube locked uploads from
unverified projects to private. Google reversed that on 2 Nov 2026. This package still keeps
uploads private by default until you set `PROJECT_AUDITED: True`, because a public video from an
unreviewed setup is the costlier mistake. That is this package's policy, not a YouTube rule. Set
`FORCE_PRIVATE_WHEN_UNAUDITED: False` to turn it off. The reason appears in `capabilities` and in
`Publication.visibility_reason`.

**Privacy is always sent.** YouTube makes a video public when `privacyStatus` is omitted, so the
adapter always sends it.

**Quota is per Google Cloud project, shared by all your users.**

| Bucket | Default | Used by |
| --- | --- | --- |
| Video Uploads | 100 `videos.insert` calls a day | `publish` |
| Default | 10,000 units a day | `channels.list`, `playlistItems.list` and `videos.list` at 1 unit each |

Days reset at midnight Pacific time. `QuotaLedger` reserves quota before each call and raises
`QuotaExhausted` with the reset time. It tracks only calls made through this package, so calls
from other software on the same Google project are not counted. Quota is released after a definite
failure and kept after `OutcomeUnknown`. A reconcile reserves its worst case of 5 units.

**OAuth.** `youtube.upload` is a sensitive scope. Apps left in Google's "Testing" mode get
short-lived refresh tokens, so plan for OAuth verification.

**Shorts.** There is no API flag. YouTube classifies vertical or square videos of up to three
minutes as Shorts. With `kind="short"` the adapter validates shape and
`SHORTS_MAX_DURATION_SECONDS`.

**Description marker.** The adapter appends `[ref:<hash>]` to each description so it can find an
upload after a lost connection. It is visible to viewers and contains only a hash.

## How failures behave

| Error | Meaning | What to do |
| --- | --- | --- |
| `AuthExpired` | Tokens no longer work | Ask the owner to reconnect |
| `CapabilityUnavailable` | The account cannot do this | Show `reason` |
| `QuotaExhausted` | No quota left | Retry the same key after `reset_at` |
| `RateLimited` | Slow down | Retry the same key after `retry_after` |
| `ValidationFailed` | Bad input; nothing was sent | Fix `issues` |
| `PlatformRejected` | A definite no | Retry the same key only if `retryable` |
| `OutcomeUnknown` | It may have succeeded | Call `reconcile`; never upload again |
| `OAuthStateInvalid` | State unknown, expired, reused or not this owner's | Restart the connection |
| `TokenDecryptionError` | No configured key fits | Restore the key that encrypted the value |

All of them inherit from `SocialPublisherError`.

Publication status moves through `pending`, `uploading`, `processing` and `published`, or ends in
`failed`. `outcome_unknown` waits for `reconcile`, which adopts the video if it finds the marker
and marks the publication `failed` after `RECONCILE_GRACE_SECONDS` if it does not.

The same idempotency key always refers to the same publication. A worker that dies mid-upload
leaves a persisted session. Calling `publish` again with the same key after the lease expires
resumes that session, and never creates a second video. Using a key for different content raises
`ValidationFailed`.

## Settings reference

All settings live in the `SOCIAL_PUBLISHER` dict and are validated at startup. Unknown keys are an
error.

| Key | Default | Purpose |
| --- | --- | --- |
| `OWNER_MODEL` | `AUTH_USER_MODEL` | `app_label.Model` that owns connected accounts |
| `TOKEN_ENCRYPTION_KEYS` | required | Fernet keys. The first encrypts; all can decrypt |
| `MEDIA_OPENER` | required | Dotted path to `callable(ref) -> binary stream` |
| `PLATFORMS` | `{}` | Per-platform settings, see below |
| `OAUTH_STATE_TTL_SECONDS` | `600` | How long a connection attempt stays valid |
| `UPLOAD_LEASE_SECONDS` | `900` | How long a silent worker keeps an upload before another can resume it |
| `RECONCILE_GRACE_SECONDS` | `3600` | How long to look for a marker before marking failed |
| `DRF_OWNER_RESOLVER` | request user | Dotted path to `callable(request) -> owner` |

`PLATFORMS["youtube"]`:

| Key | Default | Purpose |
| --- | --- | --- |
| `CLIENT_ID`, `CLIENT_SECRET`, `REDIRECT_URI` | required | Google OAuth client |
| `PROJECT_AUDITED` | `False` | Whether the project passed Google's compliance audit |
| `FORCE_PRIVATE_WHEN_UNAUDITED` | `True` | Keep uploads private until audited |
| `DAILY_UPLOAD_LIMIT` | `100` | Upload bucket size. Raise it after Google grants more |
| `DAILY_UNIT_LIMIT` | `10000` | Default bucket size |
| `SHORTS_MAX_DURATION_SECONDS` | `180` | Longest video accepted as `kind="short"` |
| `CHUNK_SIZE_BYTES` | 8 MiB | Must be a multiple of 256 KiB |
| `RETRY_BACKOFF_SECONDS` | `1.0` | Base delay when a chunk gets a 5xx response |
| `ADAPTER` | built-in | Dotted path to replace the adapter |

**Rotating keys.** Add the new key first in the list, keeping the old one. Run
`python manage.py social_publisher_rotate_keys`, then remove the old key.

## Optional extras

**DRF.** `path("social/", include("social_publisher.contrib.drf.urls"))` adds owner-scoped
endpoints for connecting, listing and disconnecting accounts, and reading capabilities and
publications. There is deliberately no browser-redirect callback. Point `REDIRECT_URI` at your
frontend and have it post `state` and `code` with the signed-in user's credentials, which stops an
attacker from tricking someone into linking their channel to the attacker's account. If you call
`complete_connection` yourself, pass `owner=` for the same reason.

**Celery.** `social_publisher.contrib.celery.tasks` has `publish_task` and `reconcile_task`. They
add no retries and set no queue, so both stay your decision.

**Signals.** `account_connected`, `account_disconnected` and `publication_status_changed` fire
after the transaction commits.

## Writing your own adapter

Implement `social_publisher.adapters.PlatformAdapter`, then register it:

```python
from social_publisher.adapters import register_adapter

register_adapter("myplatform", MyAdapter)  # called with the PLATFORMS["myplatform"] dict
```

Add a `PLATFORMS["myplatform"]` entry so the platform is enabled. What the service layer relies on:

- **Report honestly.** `publish` returns a `PublishResult` only for what the platform confirmed.
  Raise `OutcomeUnknown` when the request may have succeeded. Raise `PlatformRejected` for a
  definite no, with `retryable=True` if the same request could work later.
- **Support recovery.** Embed `request.marker` somewhere `reconcile` can search. Call
  `request.on_checkpoint(token)` before sending bytes, and resume from `request.resume_token`.
- **Handle both reconcile forms.** `publication_ref` is a media ID, or `marker:<value>` when no ID
  is known. Return `status="outcome_unknown"` when a marker is not found yet.
- **Declare quota.** `quota_for(operation)` returns a list of `QuotaCharge`, and can be empty.
  Operations are `connect`, `publish`, `reconcile` and `health`.
- **Raise `AuthExpired`** when the token is rejected. The service refreshes it once and retries.
- **Keep secrets out of errors and logs.** `TokenBundle` already hides tokens from `repr`.

## Development

```bash
pip install -e ".[celery,drf,dev]"
pytest                      # SQLite
TEST_POSTGRES_HOST=localhost pytest   # also runs the concurrent-quota test
ruff check . && mypy
```

Test fixtures in `tests/fixtures` are written by hand from Google's API reference, not recorded
from live traffic. Run them against a real test channel before relying on a new API behaviour.

## License

MIT
