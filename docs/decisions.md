# Decisions

Routine technical decisions made while building Phase 0. Platform facts were checked against
official documentation on 4 Nov 2026.

## Where the prompts and the current docs disagree

**Private uploads are now this project's policy, not a YouTube rule.** Both prompts treat the lock
on unverified projects as a hard platform rule. Google reversed it on 2 Nov 2026: the
[`videos.insert` page](https://developers.google.com/youtube/v3/docs/videos/insert) now says uploads
from unverified projects "are not restricted to private viewing mode". We kept the behaviour the
prompts ask for and made it a setting, `FORCE_PRIVATE_WHEN_UNAUDITED`, on by default. The messages
users see say it is this deployment's policy and do not blame YouTube. Set it to false to allow
public uploads from an unaudited project.

**`privacyStatus` is always sent.** The same revision makes an omitted value default to `public`.

**Upload quota is exactly 100 calls a day, at 1 unit per call,** in its own Video Uploads bucket.
The prompts say "about 100".

**`snippet.categoryId`.** The docs read during this work state it is required for
`videos.update` but do not state a requirement for insert. We send `22` (People & Blogs) unless a
category is given, which matches Google's own samples.

## django-social-video-publisher

| Decision | Why |
| --- | --- |
| Owner is a generic foreign key | A swappable FK would make the shipped migration depend on host settings. This also lets hosts attach accounts to a workspace later |
| `validate_metadata` and `quota_for` added to the adapter interface | The specified interface validates only media and has no way to declare quota, yet the spec requires both |
| `OAuthStateInvalid` and `TokenDecryptionError` added to the error list | Neither fits the specified errors, and both need distinct handling |
| Upload lease on `Publication` | Without it, two workers holding the same key could both resume one session |
| Upload session URL is encrypted, and redacted from `httpx` logs | Anyone holding that URL can act on the upload |
| Quota kept after `OutcomeUnknown`, and after a reconcile that finds nothing | Google may have charged for it. Only a definite failure releases it |
| A reconcile reserves its worst case of 5 units | The adapter cannot see the ledger per call. The ledger may slightly overcount, which is the safe direction |
| An expired session on resume raises `OutcomeUnknown` | Google says to start a new upload, but the earlier upload may have finished. Checking first avoids duplicates |
| No browser-callback view | A callback with no signed-in user allows account-linking CSRF, so `state` and `code` are posted with the user's credentials |
| `disconnect` returns whether revocation was confirmed | Local tokens are always deleted, but the host must be able to say truthfully if Google could not confirm it |
| Published as `django-social-video-publisher`, imported as `social_video_publisher` | `django-social-publisher` is taken on PyPI by an unrelated project, last released in 2016, which also installs a `social_publisher` module. Renaming both avoids a collision |
| MIT licence | Common for reusable Django apps. Change it before the first release if you prefer another |
| Support Python 3.11 to 3.14 and Django 5.2, 6.0, 6.1 | Python 3.10 reached end of life in Oct 2026. These are the maintained Django series |

## Platform

| Decision | Why |
| --- | --- |
| Anthropic SDK, `claude-opus-5-5` | Default model. Change with `ANTHROPIC_MODEL`; prices are in `ANTHROPIC_PRICES_PER_MTOK` |
| ElevenLabs for voice, Pexels for footage | Both have documented APIs. Each sits behind a provider interface and is one setting to replace |
| `providers.fake` | Runs the whole loop with no keys or spend, for tests and local work |
| `mutagen` measures voice duration | The AI worker does not need FFmpeg, so it can run the smaller image |
| Character trigrams for similarity, in Python | Works without a PostgreSQL extension or embedding costs. Replace it if false matches appear |
| Scene and subtitle timing split by word count | Works with any TTS provider. Subtitles can drift slightly within a scene compared with word-level timestamps |
| A sweep every 2 minutes drives deferred uploads | Redis redelivers long Celery ETA tasks after its visibility timeout, and a sweep also recovers from lost workers |
| Items stay `queued` with `status_detail=quota_exhausted` | The specified statuses have no quota state, and this keeps the status machine as written |
| Two extra models: `ContentEvent`, plus `discarded_cost_usd` on `ContentItem` | The audit row needs a table, and rejected scripts cost money with no script row to record it on |
| Sources are labelled unverified everywhere | The AI suggests them and nothing checks them. `sources_verified` is always false |
| Pexels credit added to each video description | Pexels asks for credit where possible |
| `cgr.dev/chainguard/minio` for local storage | The `minio/minio` and `minio/mc` repositories no longer exist on Docker Hub. The image has no shell or `mc`, so `ensure_bucket` waits for it and creates the bucket. Local development only; MinIO is AGPLv3 |
| `celery[redis]` instead of `celery` plus `redis` | `kombu`'s Redis transport needs `packaging`, which only that extra installs |
| Daily generation cap counted per UTC day | Simple and predictable for users. It is separate from YouTube's Pacific-time quota day |
