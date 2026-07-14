"""Bulk multi-select sharing (PR-B) — the pure eligibility helper and the
controller's ``bulk_share`` dispatch, all off a real QGIS install.

Covers:

* ``browser_panel.bulk_shareable`` — filters a ``selected_many()`` list to the
  OWNED, shareable subset (a shared/not-owned or container row is dropped);
* ``GeoiPlugin.bulk_share`` — one ShareDialog applied to N mixed-kind items,
  dispatching the right set-visibility + per-group share calls per kind, and
  never swallowing a per-item failure (the loop CONTINUES and the summary
  flags the failed item).

``qgis`` and the Qt widget modules are stubbed so both modules import off a
QGIS install, mirroring ``test_tile_actions`` / ``test_tiles3d_actions``.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi.geoi_client import GeoiError  # noqa: E402


class _Any:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, _n):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(m, key, val)
    sys.modules[name] = m
    return m


# --------------------------------------------------------------------------- #
# bulk_shareable — pure, imports the REAL browser_panel (qgis/Qt stubbed).
# --------------------------------------------------------------------------- #
def _import_browser_panel():
    saved = {
        k: sys.modules.get(k)
        for k in ("qgis", "qgis.core", "qgis.gui", "qgis.PyQt",
                  "qgis.PyQt.QtCore", "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
                  "geoi.gui.browser_panel")
    }
    _mod("qgis")
    _mod("qgis.core")
    _mod("qgis.gui", QgsDockWidget=_Any)
    _mod("qgis.PyQt")
    _mod("qgis.PyQt.QtCore", QSize=_Any, Qt=_Any())
    _mod("qgis.PyQt.QtGui", QIcon=_Any)
    _mod("qgis.PyQt.QtWidgets", QAbstractItemView=_Any, QGridLayout=_Any,
         QHBoxLayout=_Any, QLabel=_Any, QMenu=_Any, QPushButton=_Any,
         QSizePolicy=_Any, QStyle=_Any, QToolButton=_Any, QTreeWidget=_Any,
         QTreeWidgetItem=_Any, QVBoxLayout=_Any, QWidget=_Any)

    sys.modules.pop("geoi.gui.browser_panel", None)
    from geoi.gui import browser_panel as bp

    def cleanup():
        sys.modules.pop("geoi.gui.browser_panel", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return bp, cleanup


class BulkShareableTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = _import_browser_panel()

    def tearDown(self):
        self._cleanup()

    def test_mixed_selection_keeps_only_owned_shareable(self):
        selection = [
            ("service", {"name": "s", "title": "Roads"}, False),   # owned svc
            ("tile", {"id": 5, "title": "Ortho"}, False),          # owned tile
            ("tiles3d", {"id": 9, "title": "Cloud"}, True),        # not owned
            ("folder", {"id": "f"}, False),                        # container
        ]
        out = self.bp.bulk_shareable(selection)
        self.assertEqual([(k, p["title"]) for (k, p, _s) in out],
                         [("service", "Roads"), ("tile", "Ortho")])

    def test_all_shared_returns_empty(self):
        selection = [
            ("service", {"name": "s"}, True),
            ("tile", {"id": 5}, True),
            ("tiles3d", {"id": 9}, True),
        ]
        self.assertEqual(self.bp.bulk_shareable(selection), [])

    def test_single_eligible_item_passes_through(self):
        selection = [("project", {"id": 3, "title": "Trip"}, False)]
        out = self.bp.bulk_shareable(selection)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "project")

    def test_single_shared_item_is_empty(self):
        self.assertEqual(
            self.bp.bulk_shareable([("service", {"name": "s"}, True)]), [])


# --------------------------------------------------------------------------- #
# bulk_share — the controller dispatch, with a recording client + fake dialog.
# --------------------------------------------------------------------------- #
def _bare_plugin():
    """Import ``geoi.plugin`` with qgis + Qt stubbed — same convention as
    ``test_tile_actions._bare_plugin``."""
    saved = {
        k: sys.modules.get(k)
        for k in ("qgis", "qgis.core", "qgis.PyQt", "qgis.PyQt.QtCore",
                  "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
                  "geoi.gui.browser_panel", "geoi.gui.dialogs", "geoi.auth",
                  "geoi.tasks", "geoi.plugin")
    }
    _mod("qgis")
    _mod("qgis.core", QgsApplication=_Any, QgsProject=_Any,
         QgsRasterLayer=_Any, QgsVectorLayer=_Any)
    _mod("qgis.PyQt")
    _mod("qgis.PyQt.QtCore", Qt=_Any(), QSettings=_Any)
    _mod("qgis.PyQt.QtGui", QIcon=_Any)
    _mod("qgis.PyQt.QtWidgets", QAction=_Any, QApplication=_Any,
         QInputDialog=_Any, QMessageBox=_Any)
    sys.modules["qgis"].core = sys.modules["qgis.core"]

    for name, names in (
        ("geoi.gui.browser_panel", ["GeoiPanel"]),
        ("geoi.auth", ["SessionStore"]),
    ):
        m = types.ModuleType(name)
        for n in names:
            setattr(m, n, _Any)
        sys.modules[name] = m

    # geoi.tasks and geoi.gui.dialogs are stubbed PERMISSIVELY: any class the
    # plugin imports from them — present or a FUTURE new Task/Dialog — resolves
    # to _Any via a PEP 562 module __getattr__, so this fake never needs a
    # hand-maintained allow-list again.
    def _permissive(mod_name, **attrs):
        m = types.ModuleType(mod_name)
        for key, val in attrs.items():
            setattr(m, key, val)

        def _missing(_n, _stub=_Any):
            if _n.startswith("__") and _n.endswith("__"):
                raise AttributeError(_n)
            return _stub
        m.__getattr__ = _missing
        sys.modules[mod_name] = m
        return m

    _permissive("geoi.gui.dialogs")
    # plugin also imports the SIGNIN_DISABLED sentinel (a value) from tasks.
    _permissive("geoi.tasks", SIGNIN_DISABLED="\x00signin-disabled\x00")

    sys.modules.pop("geoi.plugin", None)
    from geoi import plugin as plugin_mod  # noqa: E402

    def cleanup():
        sys.modules.pop("geoi.plugin", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return plugin_mod, cleanup


class _RecordingClient:
    """Records every share/visibility call; raises for one nominated method so
    a mid-loop failure can be exercised."""

    def __init__(self, fail_method=None):
        self.calls = []
        self._fail_method = fail_method

    def _rec(self, method, *args):
        self.calls.append((method,) + args)
        if method == self._fail_method:
            raise GeoiError("boom", status=500)

    def update_service(self, name, visibility=None):
        self._rec("update_service", name, visibility)

    def share_service_with_group(self, name, gid):
        self._rec("share_service_with_group", name, gid)

    def update_project(self, pid, visibility=None):
        self._rec("update_project", pid, visibility)

    def share_project_with_group(self, pid, gid):
        self._rec("share_project_with_group", pid, gid)

    def set_tile_service_visibility(self, sid, vis):
        self._rec("set_tile_service_visibility", sid, vis)

    def share_tile_service_with_group(self, sid, gid):
        self._rec("share_tile_service_with_group", sid, gid)

    def set_tiles3d_visibility(self, sid, vis):
        self._rec("set_tiles3d_visibility", sid, vis)

    def share_tiles3d_with_group(self, sid, gid):
        self._rec("share_tiles3d_with_group", sid, gid)


class _FakeShareDialog:
    """Scriptable ShareDialog: returns a fixed visibility + chosen groups."""

    visibility_value = "groups"
    group_ids = [10, 20]
    accept = True

    def __init__(self, *a, **k):
        _FakeShareDialog.last_args = a

    def exec(self):
        return _FakeShareDialog.accept

    def visibility(self):
        return _FakeShareDialog.visibility_value

    def selected_group_ids(self):
        return list(_FakeShareDialog.group_ids)


class BulkShareControllerTest(unittest.TestCase):
    ITEMS = [
        ("service", {"name": "svc", "title": "Roads"}, False),
        ("tile", {"id": 5, "title": "Ortho"}, False),
        ("tiles3d", {"id": 9, "title": "Cloud"}, False),
    ]

    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()
        _FakeShareDialog.visibility_value = "groups"
        _FakeShareDialog.group_ids = [10, 20]
        _FakeShareDialog.accept = True
        self.plugin_mod.ShareDialog = _FakeShareDialog

    def tearDown(self):
        self._cleanup()

    def _plugin(self, client):
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._client = client
        p._groups = [{"id": 10, "name": "A"}, {"id": 20, "name": "B"}]
        p._signed_in = lambda: True
        p.iface = types.SimpleNamespace(mainWindow=lambda: None)
        # Run the "background" action synchronously: work() then on_ok(result).
        p._run_action = lambda desc, fn, on_ok: on_ok(fn())
        self.infos = []
        self.warns = []
        p._info = lambda text: self.infos.append(text)
        p._warn = lambda title, text: self.warns.append((title, text))
        return p

    def test_each_kind_dispatches_visibility_plus_per_group_shares(self):
        client = _RecordingClient()
        p = self._plugin(client)
        p.bulk_share(list(self.ITEMS))
        self.assertEqual(client.calls, [
            ("update_service", "svc", "groups"),
            ("share_service_with_group", "svc", 10),
            ("share_service_with_group", "svc", 20),
            ("set_tile_service_visibility", 5, "groups"),
            ("share_tile_service_with_group", 5, 10),
            ("share_tile_service_with_group", 5, 20),
            ("set_tiles3d_visibility", 9, "groups"),
            ("share_tiles3d_with_group", 9, 10),
            ("share_tiles3d_with_group", 9, 20),
        ])
        # All succeeded → one success toast, no warning box.
        self.assertEqual(self.warns, [])
        self.assertEqual(self.infos, ["Sharing updated for 3 items."])

    def test_middle_failure_does_not_stop_loop_and_is_reported(self):
        # The tile (middle item) fails on its set-visibility call.
        client = _RecordingClient(fail_method="set_tile_service_visibility")
        p = self._plugin(client)
        p.bulk_share(list(self.ITEMS))
        # The service (before) AND the tiles3d (AFTER the failure) both ran —
        # the loop CONTINUED past the failing item.
        self.assertIn(("update_service", "svc", "groups"), client.calls)
        self.assertIn(("set_tiles3d_visibility", 9, "groups"), client.calls)
        self.assertIn(("share_tiles3d_with_group", 9, 20), client.calls)
        # The tile's per-group shares were skipped (it failed before them).
        self.assertNotIn(("share_tile_service_with_group", 5, 10), client.calls)
        # A partial failure is surfaced, naming the failed item specifically.
        self.assertEqual(len(self.warns), 1)
        _title, text = self.warns[0]
        self.assertIn("Ortho", text)
        self.assertNotIn("Roads", text)
        self.assertNotIn("Cloud", text)
        # No misleading all-good toast on a partial failure.
        self.assertEqual(self.infos, [])

    def test_private_visibility_sets_only_no_group_shares(self):
        _FakeShareDialog.visibility_value = "private"
        client = _RecordingClient()
        p = self._plugin(client)
        p.bulk_share(list(self.ITEMS))
        methods = [c[0] for c in client.calls]
        self.assertEqual(methods, [
            "update_service", "set_tile_service_visibility", "set_tiles3d_visibility"])
        self.assertEqual(self.infos, ["Sharing updated for 3 items."])

    def test_cancelled_dialog_touches_nothing(self):
        _FakeShareDialog.accept = False
        client = _RecordingClient()
        p = self._plugin(client)
        p.bulk_share(list(self.ITEMS))
        self.assertEqual(client.calls, [])
        self.assertEqual(self.infos, [])
        self.assertEqual(self.warns, [])


if __name__ == "__main__":
    unittest.main()
