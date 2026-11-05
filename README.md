# Faceless Platform

Phase 0 is one working loop: a signed-in user picks a niche and topic, the system writes a script,
generates a voiceover, pulls licensed stock footage and renders a 9:16 video with burned-in
subtitles. The user reviews it and approves it, and it uploads to their YouTube channel as a Short.
Everything runs through a REST API at `/api/v1/`. There is no frontend.

| Path | What it is |
| --- | --- |
| [packages/django-social-publisher](packages/django-social-publisher/README.md) | Reusable, open-source YouTube connection and publishing app |
| [platform](platform) | The private Django project |
| [docs/decisions.md](docs/decisions.md) | Technical decisions, including where the current YouTube docs differ from the brief |
| [docs/progress.md](docs/progress.md) | What works, what was tested, and what still needs doing |

## Run it with no API keys

The fake providers generate placeholder scripts, silent audio and test-pattern video, so this costs
nothing. It needs Docker.

1. Create `.env` from the example:

   ```bash
   cp .env.example .env
   ```

   Fill in `DJANGO_SECRET_KEY` and `SOCIAL_TOKEN_ENCRYPTION_KEYS` using the commands in the file,
   then uncomment the three `*_PROVIDER` lines.

2. Start the stack:

   ```bash
   docker compose up --build
   ```

3. Open the API docs at <http://localhost:8000/api/v1/docs/>.

## Run it for real

Add these to `.env`, and remove the `*_PROVIDER` lines:

| Setting | Where it comes from |
| --- | --- |
| `ANTHROPIC_API_KEY` | [Claude Console](https://platform.claude.com/) |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | ElevenLabs account |
| `PEXELS_API_KEY` | [Pexels API](https://www.pexels.com/api/) |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` | Google Cloud: enable YouTube Data API v3 and create a "Web application" OAuth client. Add `http://localhost:8765/oauth/callback` as a redirect URI |

Then run the full loop against a **test channel**. This spends real money and uses YouTube quota:

```bash
python platform/scripts/smoke_youtube.py --api http://localhost:8000
```

## Main API flow

| Step | Request |
| --- | --- |
| Sign up and log in | `POST /auth/register/`, `POST /auth/token/` |
| Connect YouTube | `POST /social/connections/start/`, send the user to the URL, then `POST /social/connections/complete/` with `state` and `code` |
| See what the account can do | `GET /social/accounts/{id}/capabilities/`, `GET /publishing/status/` |
| Create a niche | `POST /niches/` |
| Generate a video | `POST /content/` with `niche` and `topic` |
| Follow progress and review | `GET /content/{id}/` includes the script, sources, licences and a signed preview URL |
| Decide | `POST /content/{id}/approve/` or `/reject/`, and `/disclosure/` to change the AI disclosure |
| Recover from a failure | `POST /content/{id}/retry/` |
| Check spend | `GET /usage/` |

A video is marked `published` only after `videos.list` confirms it. While the upload is under way,
`status_detail` and `status_message` explain any delay, such as `quota_exhausted` with the reset
time in `next_attempt_at`.

## Tests

```bash
# The platform, with real FFmpeg and PostgreSQL
docker compose --profile test run --rm test

# The package
cd packages/django-social-publisher
pip install -e ".[celery,drf,dev]" && pytest
```

No test calls a paid API. All external calls are mocked or use the fake providers.
