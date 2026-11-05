# Releasing django-social-video-publisher

Pushing a tag builds the package, checks it and publishes it to PyPI. Nothing is uploaded until a tag
is pushed. **PyPI never lets you reuse a version number**, even after deleting a release, so a mistake
means a new version.

## One-time setup

Add a trusted publisher so that PyPI accepts uploads from this workflow. No API tokens are needed.

Sign in at <https://pypi.org>, open <https://pypi.org/manage/account/publishing/>, choose the
**GitHub** tab and enter:

| Field | Value |
| --- | --- |
| PyPI project name | `django-social-video-publisher` |
| Owner | `NzeStan` |
| Repository name | `Faceless` |
| Workflow name | `django-social-video-publisher-release.yml` |
| Environment name | `pypi` |

You do not need to create the GitHub environment, because GitHub creates it on the first run. If you
add required reviewers to it under Settings > Environments, each release waits for your approval.

## Each release

1. Update `version` in `pyproject.toml` and `__version__` in `src/social_video_publisher/__init__.py`.
2. Add the version and date to `CHANGELOG.md`.
3. Commit, push `main`, and wait for CI to pass.
4. Tag the commit and push the tag. The tag must match the version, or the workflow stops:

   ```bash
   git tag django-social-video-publisher-v0.1.0
   git push origin django-social-video-publisher-v0.1.0
   ```

5. Watch the run in the repository's Actions tab.

## If something goes wrong

- **`invalid-publisher`:** the entry on PyPI does not match the table above. Nothing was uploaded. Fix
  the entry and use **Re-run failed jobs**.
- **The workflow fails for another reason before uploading:** nothing is on PyPI. If the fix changes
  the workflow or the code, delete the tag and tag the new commit, because a re-run uses the old commit.
- **A bad version reaches PyPI:** yank it on the project's PyPI page and release a new version. Yanking
  stops new installs from picking it, and the number stays used.
