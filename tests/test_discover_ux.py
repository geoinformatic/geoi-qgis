"""WS3c — the Discover mode + live public-search UX in the content browser.

Constructs the REAL ``GeoiPanel`` against recording Qt stubs (``panel_stub``)
and exercises:

* the "My content" | "Discover" mode switch toggling the search field;
* a keystroke debounced through the ~300 ms timer, whose timeout dispatches the
  controller's ``search_discover`` (which builds a ``DiscoverTask``);
* instant client-side filtering of already-loaded discover rows;
* ``populate_discover`` rendering results into the four category buckets, every
  leaf flagged as a read-only Discover item with the reduced 3-action menu;
* switching back to My content restoring the cached catalogue with no refetch.

A separate check proves ``tasks.DiscoverTask.work`` calls ``client.discover``
with the short optional timeout — so the debounce really dispatches a
``DiscoverTask``.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import panel_stub  # noqa: E402


class _RecCtrl:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name,) + a)
        return rec


# NOTE: the rows deliberately still carry an ``ownerName`` (which the server no
# longer sends, #1001) to PROVE the panel never reads it — a discovered row is
# labelled only by its ``created`` publication date.
_RESULTS = {
    "services": [{"name": "roads", "title": "Roads", "visibility": "public",
                  "ownerName": "Ada", "created": "2026-07-01T09:00:00Z"}],
    "projects": [{"id": 1, "name": "Trip", "visibility": "public",
                  "ownerName": "Ada", "created": "2026-07-02T09:00:00Z"}],
    "tiles": [{"id": 2, "title": "Ortho", "visibility": "public",
               "ownerName": "Ben", "created": "2026-07-03T09:00:00Z"}],
    "tiles3d": [{"id": 3, "title": "Scan", "visibility": "public",
                 "ownerName": "Cy", "created": "2026-07-04T09:00:00Z"}],
}


class DiscoverModeSwitchTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = panel_stub.install()
        self.ctrl = _RecCtrl()
        self.panel = self.bp.GeoiPanel(self.ctrl)

    def tearDown(self):
        self._cleanup()

    def test_search_field_hidden_in_my_content_by_default(self):
        self.assertFalse(self.panel._search.isVisible())
        self.assertEqual(self.panel._mode, "mine")

    def test_switching_to_discover_reveals_search(self):
        self.panel._mode_disc.clicked.emit()
        self.assertTrue(self.panel._search.isVisible())
        self.assertEqual(self.panel._mode, "discover")

    def test_switching_back_hides_search(self):
        self.panel._set_mode("discover")
        self.panel._mode_mine.clicked.emit()
        self.assertFalse(self.panel._search.isVisible())
        self.assertEqual(self.panel._mode, "mine")


class DiscoverDebounceTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = panel_stub.install()
        self.ctrl = _RecCtrl()
        self.panel = self.bp.GeoiPanel(self.ctrl)
        self.panel._set_mode("discover")

    def tearDown(self):
        self._cleanup()

    def test_keystroke_starts_the_debounce_timer_without_immediate_fetch(self):
        self.ctrl.calls = []
        self.panel._search.setText("road")
        self.panel._search.textChanged.emit("road")
        self.assertGreaterEqual(self.panel._search_timer.started, 1)
        # No QUERY search dispatched yet — only when the timer times out.
        self.assertNotIn(("search_discover", "road"), self.ctrl.calls)

    def test_timeout_dispatches_search_discover(self):
        self.panel._search.setText("road")
        self.panel._search.textChanged.emit("road")
        self.panel._search_timer.timeout.emit()
        self.assertIn(("search_discover", "road"), self.ctrl.calls)

    def test_empty_query_loads_the_full_page_not_the_query_debounce(self):
        # F2 (#1001): an empty box loads ALL categories immediately (an
        # empty-query fetch) and does NOT arm the per-query debounce.
        self.ctrl.calls = []
        self.panel._search.setText("")
        self.panel._search.textChanged.emit("")
        self.assertIn(("search_discover", ""), self.ctrl.calls)
        # A subsequent timer tick with the box still empty dispatches nothing.
        self.ctrl.calls = []
        self.panel._search_timer.timeout.emit()
        self.assertEqual(
            [c for c in self.ctrl.calls if c[0] == "search_discover"], [])

    def test_client_side_filter_narrows_loaded_rows_instantly(self):
        # After results are loaded, typing filters the tree WITHOUT a refetch.
        self.panel.populate_discover(_RESULTS, query="")
        # "ortho" only matches the tile row.
        self.panel._search.setText("ortho")
        self.panel._on_search_changed("ortho")
        titles = [it.text(0) for it in self.panel._tree.top]
        self.assertEqual(titles, ["Tile Services"])


class PopulateDiscoverTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = panel_stub.install()
        self.panel = self.bp.GeoiPanel(_RecCtrl())

    def tearDown(self):
        self._cleanup()

    def test_four_category_buckets_rendered(self):
        self.panel.populate_discover(_RESULTS, query="")
        self.assertEqual(
            [it.text(0) for it in self.panel._tree.top],
            ["Feature Services", "Web Maps", "Tile Services",
             "3D Tiles Services"])

    def test_every_leaf_is_a_read_only_discover_item(self):
        self.panel.populate_discover(_RESULTS, query="")
        for cat in self.panel._tree.top:
            leaf = cat.child(0)
            self.assertTrue(leaf.data(0, self.bp.ROLE_DISCOVER),
                            "discover leaf must carry ROLE_DISCOVER")

    def test_reduced_menu_for_each_discovered_kind(self):
        for kind in ("service", "project", "tile", "tiles3d"):
            self.assertEqual(
                self.bp.actions_for(kind, discover=True),
                ["Add to map", "Copy URL", "Open in geoi"])

    def test_empty_results_show_no_match_status(self):
        # The search box must hold the query the response is for — populate_discover
        # drops a stale payload whose query no longer matches the live box.
        self.panel._search.setText("zzz")
        self.panel.populate_discover(
            {"services": [], "projects": [], "tiles": [], "tiles3d": []},
            query="zzz")
        self.assertIn("No public content matches 'zzz'",
                      self.panel._status.text())

    def test_populate_discover_drops_stale_response(self):
        # A slow earlier query (q1) resolving after the box moved on to q2 must
        # NOT paint q1's rows (guards the debounced out-of-order race).
        self.panel._search.setText("q2")
        self.panel.populate_discover(_RESULTS, query="q1")
        kinds = [it.data(0, self.bp.ROLE_KIND) for it in self.panel._tree.top]
        self.assertEqual(kinds, [])  # stale payload ignored

    def test_no_folders_or_shared_section(self):
        self.panel.populate_discover(_RESULTS, query="")
        kinds = [it.data(0, self.bp.ROLE_KIND) for it in self.panel._tree.top]
        self.assertNotIn("folder", kinds)
        self.assertNotIn("shared", kinds)
        self.assertTrue(all(k == "category" for k in kinds))


class ModeCacheTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = panel_stub.install()
        self.ctrl = _RecCtrl()
        self.panel = self.bp.GeoiPanel(self.ctrl)

    def tearDown(self):
        self._cleanup()

    def test_switching_back_to_mine_restores_cache_without_refetch(self):
        catalog = {
            "folders": [],
            "ownerId": 7,
            "services": [{"name": "s", "title": "Roads",
                          "owner": {"id": 7}, "visibility": "private"}],
            "projects": [], "tiles": [], "tiles3d": [],
        }
        self.panel.populate(catalog)  # caches + renders My content
        self.panel._set_mode("discover")
        self.panel.populate_discover(_RESULTS, query="")
        self.assertEqual([it.text(0) for it in self.panel._tree.top][0],
                         "Feature Services")
        # Back to My content: the cached catalogue re-renders; the controller's
        # refresh() is NEVER called (no refetch).
        self.panel._set_mode("mine")
        self.assertEqual([it.text(0) for it in self.panel._tree.top],
                         ["Feature Services"])
        self.assertNotIn("refresh", [c[0] for c in self.ctrl.calls])


# ------------------------------------------------ DiscoverTask off the UI thread
def _import_tasks():
    saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.core")}
    qgis = types.ModuleType("qgis")
    qgis_core = types.ModuleType("qgis.core")

    class _QgsTask:
        def __init__(self, *a, **k):
            pass

        def setProgress(self, *_a):
            pass

        def isCanceled(self):
            return False

    qgis_core.QgsTask = _QgsTask
    qgis.core = qgis_core
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = qgis_core
    sys.modules.pop("geoi.tasks", None)
    from geoi import tasks

    def cleanup():
        sys.modules.pop("geoi.tasks", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return tasks, cleanup


class DiscoverTaskTest(unittest.TestCase):
    def setUp(self):
        self.tasks, self._cleanup = _import_tasks()

    def tearDown(self):
        self._cleanup()

    def test_work_calls_client_discover_with_short_timeout(self):
        seen = {}

        class _Client:
            def discover(self, q="", kind="all", limit=25, offset=0,
                         timeout=None):
                seen.update(q=q, kind=kind, limit=limit, offset=offset,
                            timeout=timeout)
                return {"services": [], "projects": [],
                        "tiles": [], "tiles3d": []}

        task = self.tasks.DiscoverTask(_Client(), "road", lambda ok, p: None,
                                       kind="tiles", limit=10, offset=5)
        result = task.work()
        self.assertEqual(seen["q"], "road")
        self.assertEqual(seen["kind"], "tiles")
        self.assertEqual(seen["limit"], 10)
        self.assertEqual(seen["offset"], 5)
        self.assertEqual(seen["timeout"], self.tasks.DiscoverTask.OPTIONAL_TIMEOUT)
        self.assertLess(self.tasks.DiscoverTask.OPTIONAL_TIMEOUT, 30)
        self.assertEqual(set(result),
                         {"services", "projects", "tiles", "tiles3d"})


if __name__ == "__main__":
    unittest.main()
