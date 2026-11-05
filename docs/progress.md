# Progress

Status on 5 Nov 2026. **Phase 0 is not done yet.** The done criteria need a real upload to a real
channel, and that has not happened because it needs your API keys and a test channel.

## What has been run

| Check | Result |
| --- | --- |
| Package tests, on PostgreSQL | 83 passed, including the concurrent quota test |
| Package type checks and lint | mypy, ruff check and ruff format clean |
| Package build | Wheel and sdist built; the wheel includes the migration and `py.typed` |
| Platform tests in Docker, with real FFmpeg and PostgreSQL | 78 passed |
| Platform tests on Windows, after the fixes below | 76 passed, 2 skipped because FFmpeg is not on PATH |
| Platform lint and migrations | ruff clean, no missing migrations |
| Production image | Broker import and `manage.py check --deploy` clean with production settings |
| Dependency audit | pip-audit found no known vulnerabilities |
| Full Docker stack with fake providers | Passed, see below |

The Docker platform suite ran before the `celery[redis]` change. That change only adds dependencies,
and the Windows suite and the full stack both ran after it.

### Full-stack run

This used real Celery workers, PostgreSQL, Redis, MinIO and FFmpeg, driven over HTTP:

1. Sign up, log in, create a niche and create a content item.
2. The item moved through `draft`, `scripting` and `rendering` to `awaiting_review`.
3. The signed preview URL returned the video. It is 1080x1920 H.264 with AAC audio, and a frame
   checked by eye showed legible burned-in subtitles.
4. A tampered signature got 403, another user got 404, and no credentials got 401.
5. `/usage/` and `/publishing/status/` returned the expected values.

Approval and upload were not part of this run, because they need a connected YouTube account.

## Bugs found only by running the real stack

Neither of these showed up in the test suite.

- **The stack would not start on a clean machine.** The `minio/minio` and `minio/mc` repositories no
  longer exist on Docker Hub. It now uses `cgr.dev/chainguard/minio`, and the bucket is created by a
  new `ensure_bucket` command.
- **Every Celery worker crashed on startup.** `kombu`'s Redis transport needs `packaging`, which the
  test image installed only by accident. The dependency is now `celery[redis]`, and a new CI job
  checks the production image without dev tools. That job's checks passed locally, but the job has
  not run on GitHub.

## What has not been run

| Not run | What it leaves unproven |
| --- | --- |
| Any live API call: Anthropic, ElevenLabs, Pexels, YouTube | That our requests match the real services. All of this is mocked |
| `platform/scripts/smoke_youtube.py` | The whole approve-and-upload loop against YouTube. It has never been executed |
| GitHub Actions | That the workflows pass, including the Python and Django matrix and the release workflow |
| Package install matrix | Only Python 3.14 with Django 6.1 has been tested locally |

**The YouTube test fixtures are hand-written from the documentation, not recorded.** The tests show
the code handles the documented behaviour. If the real responses differ, the tests will not notice.

Upload, resume, quota, reconcile and forced-private behaviour are covered by tests against those
mocks only.

## Before going live

1. Decide whether to keep `FORCE_PRIVATE_WHEN_UNAUDITED` on. See [decisions.md](decisions.md).
2. Read the Pexels licence. The page refused automated access, so its terms were not checked.
3. Run the smoke script against a test channel, then replace the hand-written fixtures with recorded
   responses.
4. Replace the local-only MinIO credentials and the development server used in `docker-compose.yml`.
   That file is for local use and is not a production deployment.
