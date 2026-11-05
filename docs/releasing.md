# Releasing django-social-video-publisher

Pushing a tag builds the package and publishes it to TestPyPI, then to PyPI. Nothing is uploaded
until a tag is pushed. **PyPI never lets you reuse a version number**, even after deleting a release,
so a mistake means a new version.

## One-time setup

You only do this before the first release. No API tokens are needed, because PyPI trusts this
workflow directly.

1. **Add a pending publisher on PyPI.** Sign in to <https://pypi.org>, turn on 2FA if asked, then open
   <https://pypi.org/manage/account/publishing/> and add:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `django-social-video-publisher` |
   | Owner | `NzeStan` |
   | Repository name | `Faceless` |
   | Workflow name | `django-social-video-publisher-release.yml` |
   | Environment name | `pypi` |

2. **Do the same on TestPyPI.** It is a separate account at <https://test.pypi.org>. Use
   <https://test.pypi.org/manage/account/publishing/>, with the same values except the environment
   name, which is `testpypi`.

3. **Create the two GitHub environments.** In the repository, go to Settings > Environments and create
   `testpypi` and `pypi`.

4. **Add an approval step to `pypi`.** Tick "Required reviewers" and add yourself. The release then
   stops after TestPyPI and waits for you, so you can check it before it goes live.

## Each release

1. Update `version` in `pyproject.toml` and `__version__` in `src/social_video_publisher/__init__.py`.
2. Add the version and date to `CHANGELOG.md`.
3. Commit, push `main`, and wait for CI to pass.
4. Tag the commit and push the tag. The tag must match the version, or the workflow stops:

   ```bash
   git tag django-social-video-publisher-v0.1.0
   git push origin django-social-video-publisher-v0.1.0
   ```

5. Watch the run in the repository's Actions tab until it pauses at `pypi`.
6. Check the TestPyPI version in a clean environment:

   ```bash
   python -m venv /tmp/check && . /tmp/check/bin/activate
   pip install --index-url https://test.pypi.org/simple/ \
     --extra-index-url https://pypi.org/simple/ "django-social-video-publisher==0.1.0"
   python -c "import social_video_publisher as p; print(p.__version__)"
   ```

7. Approve the `pypi` job in the Actions tab. This is the step you cannot undo.

## If something goes wrong

- **The workflow fails before `pypi`:** nothing is on PyPI. Fix it, delete the tag, and tag again. A
  re-run skips files TestPyPI already has, but those files are not replaced, so bump the version if
  the package contents changed.
- **A bad version reaches PyPI:** yank it on the project's PyPI page and release a new version. Yanking
  stops new installs from picking it, and the number stays used.
