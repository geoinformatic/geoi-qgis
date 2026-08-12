# Contributing to the geoi QGIS plugin

Thanks for taking the time to contribute! This plugin is small, dependency-free
and well-tested — please help keep it that way.

## Ground rules

- **No third-party dependencies.** The plugin uses only the Python standard
  library and the QGIS API. Don't add `pip` requirements.
- **QGIS 3 and 4, Qt5 and Qt6.** Code must run on QGIS 3.22 LTR through the
  latest release. Use `.exec()` (not the Qt5-only `.exec_()`); CI rejects the
  latter.
- **Keep QGIS imports lazy.** The pure-logic modules (`geoi_client.py`,
  `convert.py`, `auth.py`, `content_tree.py`) import nothing from QGIS so they
  stay unit-testable without a QGIS install. Put QGIS-touching code in the GUI
  and task adapters.
- **Never log or embed the session token** in a URL or a log message.

## Develop & test

```bash
# unit tests — pure modules, no QGIS needed
python3 -m unittest discover -s tests -p 'test_*.py'

# lint
flake8 --max-line-length=100 --extend-ignore=E203,W503 geoi tests

# security scan — plugins.qgis.org runs the same gate on upload
bandit -r geoi           # must report 0 issues

# build the installable zip
scripts/package.sh        # -> dist/geoi.zip
```

All of the above run in CI on every pull request, across QGIS 3.28 / 3.34 /
latest. The `bandit` scan mirrors the automated check plugins.qgis.org runs
when the plugin is uploaded, so a green CI run means the release won't be
blocked on it. Findings that are reviewed and intentional (e.g. deliberate
fail-soft `try/except/pass`) must carry an inline `# nosec <ID>` with a short
`# security review:` justification — never blanket-disable a test.

## Pull requests

1. Fork and create a topic branch.
2. **One concern per PR.** Keep unrelated fixes, refactors and features in
   separate pull requests — small, focused PRs are reviewed and merged much
   faster.
3. **Use [Conventional Commits](https://www.conventionalcommits.org/)** for
   commit messages and the PR title: `feat: …`, `fix: …`, `docs: …`, `ci: …`,
   `test: …`, `refactor: …`.
4. Add or update tests for your change.
5. Make sure `flake8`, `bandit` and the unit tests pass locally (CI runs the
   same commands, see above).
6. Update `CHANGELOG.md` and bump `version` in `geoi/metadata.txt` if your
   change is user-visible. Note that merging a version bump to `main` cuts a
   release automatically (see `.github/workflows/release.yml`) — maintainers
   handle the bump if you'd rather leave it out.
7. Open the PR with a clear description of the problem and the fix, linking
   any related issue.

By contributing you agree that your contributions are licensed under the
project's [AGPL-3.0](LICENSE) license.
