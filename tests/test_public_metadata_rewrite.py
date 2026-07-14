"""Regression lock for the public-metadata URL rewrite (WS-RELEASE-GATE, #1001).

A wrong-URL upload has already blocked a plugin release, so this pins that the
publish transformation produces EXACTLY the three known-good geoi-qgis URLs from
a monorepo-values input, and leaves version / icon / license / tags / changelog
byte-identical.

Pure Python (no QGIS) — runs standalone:
    python3 -m unittest tests.test_public_metadata_rewrite

`tools/` is geoi-only (the publish workflow syncs `geoi/`, `tests/` and
`scripts/` to geoi-qgis, never `tools/`), so this file SKIPS there instead of
failing to import.
"""

import os
import sys
import unittest

# Make `tools/` importable when run from qgis-plugin/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from tools.rewrite_public_metadata import rewrite, PUBLIC_URLS
except ImportError:
    rewrite = PUBLIC_URLS = None


# A representative metadata.txt with the MONOREPO url values (what the committed
# file carries). Field order + surrounding fields mirror the real file so the
# "leaves everything else untouched" assertions are meaningful.
GEOI_INPUT = """[general]
name=geoi
qgisMinimumVersion=3.22
qgisMaximumVersion=4.99
description=Sign in to your geoi platform.
about=The geoi plugin connects QGIS to a geoi platform.
version=1.6.0
author=geoinformatic
email=mail@geoi.de
tags=geoi,feature service,arcgis,web,cloud,publish
homepage=https://geoi.de
tracker=https://github.com/geoinformatic/geoi/issues
repository=https://github.com/geoinformatic/geoi
category=Web
icon=icon.png
experimental=False
deprecated=False
license=AGPL-3.0
changelog=1.6.0 — Redesigned action bar. 1.5.0 — Manage groups from QGIS.
"""


@unittest.skipUnless(
    rewrite is not None,
    "tools/rewrite_public_metadata is geoi-only (not synced to geoi-qgis) — skip there",
)
class RewritePublicMetadata(unittest.TestCase):
    def setUp(self):
        self.out = rewrite(GEOI_INPUT)
        self.lines = self.out.split("\n")

    def _value(self, key):
        for line in self.lines:
            if line.startswith(key + "="):
                return line[len(key) + 1:]
        return None

    # --- the three URL fields become EXACTLY the public values -------------
    def test_homepage_rewritten_exactly(self):
        self.assertEqual(
            self._value("homepage"),
            "https://github.com/geoinformatic/geoi-qgis#usage",
        )

    def test_tracker_rewritten_exactly(self):
        self.assertEqual(
            self._value("tracker"),
            "https://github.com/geoinformatic/geoi-qgis/issues",
        )

    def test_repository_rewritten_exactly(self):
        self.assertEqual(
            self._value("repository"),
            "https://github.com/geoinformatic/geoi-qgis",
        )

    def test_values_match_the_pinned_constant(self):
        self.assertEqual(self._value("homepage"), PUBLIC_URLS["homepage"])
        self.assertEqual(self._value("tracker"), PUBLIC_URLS["tracker"])
        self.assertEqual(self._value("repository"), PUBLIC_URLS["repository"])

    # --- every OTHER field is byte-identical -------------------------------
    def test_other_fields_unchanged(self):
        for key in ("name", "qgisMinimumVersion", "qgisMaximumVersion",
                    "description", "about", "version", "author", "email",
                    "tags", "category", "icon", "experimental", "deprecated",
                    "license", "changelog"):
            in_val = None
            for line in GEOI_INPUT.split("\n"):
                if line.startswith(key + "="):
                    in_val = line[len(key) + 1:]
                    break
            self.assertEqual(
                self._value(key), in_val,
                "field %r must be left untouched by the rewrite" % key,
            )

    def test_version_icon_license_tags_changelog_exact(self):
        self.assertEqual(self._value("version"), "1.6.0")
        self.assertEqual(self._value("icon"), "icon.png")
        self.assertEqual(self._value("license"), "AGPL-3.0")
        self.assertTrue(self._value("tags").startswith("geoi,feature service"))
        self.assertTrue(self._value("changelog").startswith("1.6.0 — Redesigned"))

    # --- structural: only three lines differ, nothing added/removed --------
    def test_only_three_lines_change(self):
        in_lines = GEOI_INPUT.split("\n")
        self.assertEqual(len(in_lines), len(self.lines),
                         "no line may be added or removed")
        differing = [
            i for i, (a, b) in enumerate(zip(in_lines, self.lines)) if a != b
        ]
        self.assertEqual(len(differing), 3,
                         "exactly the 3 URL lines must differ")

    def test_trailing_newline_preserved(self):
        self.assertTrue(GEOI_INPUT.endswith("\n"))
        self.assertTrue(self.out.endswith("\n"))

    # --- idempotent: rewriting already-public metadata is a no-op ----------
    def test_idempotent(self):
        self.assertEqual(rewrite(self.out), self.out)


if __name__ == "__main__":
    unittest.main()
