# Vendored third-party code

This directory bundles a small, pure-Python dependency so the geoi QGIS
plugin works the instant it is installed — with **no** separate `pip` /
`go install` step.

## `pmtiles/` — Protomaps PMTiles Python library

- **Upstream:** `pmtiles` on PyPI — https://pypi.org/project/pmtiles/
- **Version:** `3.7.0` (pinned)
- **Source artifact:** `pmtiles-3.7.0.tar.gz` (sdist),
  sha256 `ed8b04d550d104c81a759c9cc07bfc5743750f62e751f840425f4b9f4bb903df`
- **Vendored files** (byte-identical to the sdist's `pmtiles/` package, with
  one deliberate patch — see below): `__init__.py`, `convert.py`,
  `reader.py`, `writer.py`, `tile.py`, `v2.py`, `py.typed`. The sdist's
  `bin/`, `examples/`, `test/` and `*.egg-info` are intentionally NOT
  vendored.
- **Deliberate patch (2026-07, security-scan fix):** `v2.py`'s legacy header check used
  `assert magic == 0x4D50`. `assert` statements are stripped when Python
  runs under `-O`, so the PMTiles v2 magic-number check would silently
  vanish in an optimised interpreter — and plugins.qgis.org's Bandit
  security gate (B101, "use of assert detected") blocks any plugin upload
  that contains one, vendored or not. Replaced with an explicit
  `if ...: raise ValueError(...)` that is byte-for-byte equivalent at
  runtime (same check, same failure mode) and cannot be optimised away.
  This is the only intentional deviation from the upstream sdist; every
  other vendored file is untouched.
- **License:** BSD-3-Clause — see `pmtiles/LICENSE` (the upstream sdist ships
  no LICENSE file, so the canonical text from the protomaps/PMTiles repo is
  added here for provenance). geoi (AGPL-3.0) redistributing BSD-3 code is
  compatible; the upstream notice is preserved.
- **Why bundled:** the raster→PMTiles publish path needs an MBTiles→PMTiles
  writer. `geoi/raster.py` prefers a native `pmtiles` CLI or a user-installed
  `pmtiles` package when present, and falls back to THIS vendored copy so
  publishing always works out of the box.
- **Purity / safety:** stdlib-only (gzip, json, sqlite3, mmap, tempfile, io,
  os, shutil, contextlib, enum, collections, typing). No native build, no
  network, no `subprocess`/`eval`/`exec`. The only filesystem effects are
  reading the caller's local `.mbtiles` and writing the caller's local
  `.pmtiles`.

## Updating

Do NOT hand-edit the vendored files. To update: `pip download pmtiles==<ver>
--no-deps --no-binary :all:`, extract, copy the `pmtiles/` modules here,
refresh the version + sha256 above, and re-run the plugin test suite.
