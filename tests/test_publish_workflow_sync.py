"""Structural guard for the one-click publish workflow's tree sync.

geoi-qgis's own CI (`ci.yml`) runs the `tests/` suite (sibling of `geoi/`) and
builds the zip via `scripts/package.sh`. The publish workflow used to rsync ONLY
the `geoi/` package folder to geoi-qgis, so its `tests/` drifted into a stale
fossil and geoi-qgis CI broke (`ImportError: cannot import name 'BasemapsTask'`).
This pins that the workflow ALSO syncs the current `tests/` (and `scripts/`) from
the checkout, with `--delete`, so the fossil can never come back.

Pure Python (no QGIS) — runs standalone:
    python3 -m unittest tests.test_publish_workflow_sync

This test file is synced INTO geoi-qgis by the very sync it guards, but the
workflow itself is geoi-only (never synced), so the test SKIPS there.
"""

import os
import re
import unittest

# qgis-plugin/tests/ -> qgis-plugin/ -> repo root, then .github/workflows/.
WORKFLOW_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".github", "workflows", "publish-qgis-public.yml",
)


@unittest.skipUnless(
    os.path.exists(WORKFLOW_PATH),
    "publish-qgis-public.yml is geoi-only (not synced to geoi-qgis) — skip there",
)
class PublishWorkflowSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW_PATH, encoding="utf-8") as fh:
            cls.text = fh.read()

    def test_syncs_current_tests_dir(self):
        # An rsync invocation sourcing the checked-out tests/ into $dst/tests/.
        # The rsync flags may sit on a preceding backslash-continued line, so
        # match the source→dest pair (which share one line) and confirm an
        # rsync keyword precedes it.
        m = re.search(r'qgis-plugin/tests/"\s+"\$dst/tests/', self.text)
        self.assertIsNotNone(
            m, "publish workflow must rsync qgis-plugin/tests/ -> $dst/tests/")
        preceding = self.text[max(0, m.start() - 200): m.start()]
        self.assertIn("rsync", preceding,
                      "the tests/ source→dest pair must be an rsync invocation")

    def test_tests_sync_uses_delete(self):
        # The tests rsync must carry --delete so geoi-qgis fossils are reconciled.
        m = re.search(r'qgis-plugin/tests/"\s+"\$dst/tests/', self.text)
        self.assertIsNotNone(m)
        # --delete lives on the same rsync invocation, on the flags line just
        # before the source→dest pair; check the surrounding block.
        block = self.text[max(0, m.start() - 200): m.end()]
        self.assertIn("--delete", block,
                      "tests/ rsync must use --delete to reconcile stale files")

    def test_sources_tests_from_checkout_not_pubdir(self):
        # Must source from the checkout ($(pwd)/qgis-plugin/tests/), never
        # $PUB_DIR (which only holds the rewritten geoi/ package).
        self.assertIn("$(pwd)/qgis-plugin/tests/", self.text)
        self.assertNotRegex(
            self.text,
            r"\$PUB_DIR[^\n]*tests/",
            "tests/ must not be sourced from $PUB_DIR",
        )


if __name__ == "__main__":
    unittest.main()
