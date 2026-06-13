## What & why

<!-- What does this change and why? Link any related issue. -->

## Checklist

- [ ] Unit tests pass: `python3 -m unittest discover -s tests -p 'test_*.py'`
- [ ] Lint passes: `flake8 --max-line-length=100 --extend-ignore=E203,W503 geoi tests`
- [ ] Works on QGIS 3 and 4 (no Qt5-only `.exec_()`)
- [ ] No new third-party dependencies
- [ ] `CHANGELOG.md` and `geoi/metadata.txt` version updated if user-visible
