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

# build the installable zip
scripts/package.sh        # -> dist/geoi.zip
```

All of the above run in CI on every pull request, across QGIS 3.28 / 3.34 /
latest.

## Pull requests

1. Fork and create a topic branch.
2. Add or update tests for your change.
3. Make sure `flake8` and the unit tests pass locally.
4. Update `CHANGELOG.md` and bump `version` in `geoi/metadata.txt` if your
   change is user-visible.
5. Open a PR with a clear description of the problem and the fix.

By contributing you agree that your contributions are licensed under the
project's [Apache-2.0](LICENSE) license.
