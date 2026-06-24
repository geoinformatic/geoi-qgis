"""Browser add-flows: tile deep links, TOC ordering, and double-click routing.

Covers the QGIS-plugin content-browser improvements:

* #8 — ``open_tile_in_web_app`` builds ``<base>/?addtile=<id>`` and appends
  ``&ttoken=<shareToken>`` ONLY for a non-public service.
* #9 — feature services insert at the TOP of the layer tree, tile services at
  the BOTTOM; double-clicking a tile node adds it as a tile layer.

All pure / stdlib; ``qgis`` (and the Qt widget modules the plugin imports) are
stubbed so ``geoi.plugin`` imports off a QGIS install, mirroring
``test_tile_fixes`` which already builds a bare plugin instance this way.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bare_plugin():
    """Import ``geoi.plugin`` with qgis + Qt stubbed and return ``(mod, cleanup)``
    plus a recorder for the ``QgsProject`` layer-tree calls."""
    saved = {
        k: sys.modules.get(k)
        for k in ("qgis", "qgis.core", "qgis.PyQt", "qgis.PyQt.QtCore",
                  "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets")
    }

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for key, val in attrs.items():
            setattr(m, key, val)
        sys.modules[name] = m
        return m

    class _Any:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, _n):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

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
        ("geoi.gui.dialogs", ["FeedbackDialog", "MoveToFolderDialog",
                              "PublishDialog", "PublishRasterDialog",
                              "SaveProjectDialog", "SettingsDialog",
                              "ShareDialog"]),
        ("geoi.auth", ["SessionStore"]),
        ("geoi.tasks", ["ActionTask", "CatalogTask", "PublishTask",
                        "RasterPublishTask", "SaveProjectTask", "SignInTask"]),
    ):
        m = types.ModuleType(name)
        for n in names:
            setattr(m, n, _Any)
        sys.modules[name] = m
    # plugin also imports the SIGNIN_DISABLED sentinel from tasks.
    sys.modules["geoi.tasks"].SIGNIN_DISABLED = "\x00signin-disabled\x00"

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


class _FakeTileClient:
    """A client stub returning a scripted tile-service detail."""

    def __init__(self, base_url, detail):
        self.base_url = base_url
        self._detail = detail

    def tile_service(self, service_id):
        return dict(self._detail, id=service_id)


# --------------------------------------------------------------------- #8
class TileDeepLinkTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _plugin(self, base_url, detail, warned):
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._client = _FakeTileClient(base_url, detail)
        p._warn = lambda *a: warned.append(a)
        return p

    def test_public_service_addtile_only(self):
        warned = []
        p = self._plugin(
            "https://www.geoi.de",
            {"visibility": "public", "shareToken": "ignored"}, warned)
        url, err = p.tile_deeplink_url({"id": 42})
        self.assertIsNone(err)
        self.assertEqual(url, "https://www.geoi.de/?addtile=42")
        self.assertNotIn("ttoken", url)
        self.assertEqual(warned, [])

    def test_private_service_appends_ttoken(self):
        warned = []
        p = self._plugin(
            "https://staging.geoi.de",
            {"visibility": "private", "shareToken": "tok/with?chars"}, warned)
        url, err = p.tile_deeplink_url({"id": "7"})
        self.assertIsNone(err)
        # absolute base, addtile=<id>, then the URL-encoded share token.
        self.assertTrue(url.startswith("https://staging.geoi.de/?addtile=7"))
        self.assertIn("&ttoken=tok%2Fwith%3Fchars", url)

    def test_no_id_is_an_error(self):
        warned = []
        p = self._plugin("https://www.geoi.de", {}, warned)
        url, err = p.tile_deeplink_url({})
        self.assertEqual(url, "")
        self.assertTrue(err)


# --------------------------------------------------------------------- #9
class _Layer:
    def __init__(self, valid=True):
        self._valid = valid

    def isValid(self):
        return self._valid


class _FakeRoot:
    """Records insertLayer / addLayer calls to assert TOC placement."""

    def __init__(self):
        self.calls = []

    def insertLayer(self, index, layer):  # noqa: N802 - QGIS name
        self.calls.append(("insert", index, layer))

    def addLayer(self, layer):  # noqa: N802 - QGIS name
        self.calls.append(("append", layer))


class _FakeProject:
    def __init__(self, root):
        self._root = root
        self.registered = []

    def addMapLayer(self, layer, add_to_legend=True):  # noqa: N802 - QGIS name
        self.registered.append((layer, add_to_legend))

    def layerTreeRoot(self):  # noqa: N802 - QGIS name
        return self._root


class TocOrderingTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _plugin(self, project):
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        # Point the module-level QgsProject.instance() at our fake.
        self.plugin_mod.QgsProject = types.SimpleNamespace(
            instance=lambda: project)
        return p

    def test_feature_service_inserts_at_top(self):
        root = _FakeRoot()
        project = _FakeProject(root)
        p = self._plugin(project)
        layer = _Layer()
        p._add_layer_to_toc(layer, top=True)
        # Registered WITHOUT auto-placing, then inserted at index 0 (top).
        self.assertEqual(project.registered, [(layer, False)])
        self.assertEqual(root.calls, [("insert", 0, layer)])

    def test_tile_service_appends_at_bottom(self):
        root = _FakeRoot()
        project = _FakeProject(root)
        p = self._plugin(project)
        layer = _Layer()
        p._add_layer_to_toc(layer, top=False)
        self.assertEqual(project.registered, [(layer, False)])
        self.assertEqual(root.calls, [("append", layer)])


# ---------------------------------------------------- double-click routing
class _FakeController:
    def __init__(self):
        self.calls = []

    def add_service(self, payload):
        self.calls.append(("service", payload))

    def add_tile_layer(self, payload):
        self.calls.append(("tile", payload))

    def add_project(self, payload):
        self.calls.append(("project", payload))


def _import_browser_panel():
    """Import ``geoi.gui.browser_panel`` with qgis + all the Qt widget names it
    imports at module load stubbed, returning ``(module, cleanup)``."""
    qgis_names = (
        "qgis", "qgis.gui", "qgis.PyQt", "qgis.PyQt.QtCore",
        "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
    )
    saved = {k: sys.modules.get(k) for k in qgis_names}

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

    _mod("qgis")
    _mod("qgis.gui", QgsDockWidget=_Any)
    _mod("qgis.PyQt")
    _mod("qgis.PyQt.QtCore", QSize=_Any, Qt=_Any())
    _mod("qgis.PyQt.QtGui", QIcon=_Any)
    _mod("qgis.PyQt.QtWidgets", **{
        n: _Any for n in (
            "QAbstractItemView", "QGridLayout", "QHBoxLayout", "QLabel",
            "QMenu", "QPushButton", "QSizePolicy", "QStyle", "QToolButton",
            "QTreeWidget", "QTreeWidgetItem", "QVBoxLayout", "QWidget",
        )
    })
    sys.modules.pop("geoi.gui.browser_panel", None)
    import importlib
    bp = importlib.import_module("geoi.gui.browser_panel")

    def cleanup():
        sys.modules.pop("geoi.gui.browser_panel", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return bp, cleanup


class DoubleClickRoutingTest(unittest.TestCase):
    """``_add_to_map`` routes each kind to the right controller add call —
    pure: it never touches Qt, so we call it on a bare panel instance."""

    def test_routes_service_tile_and_project(self):
        bp, cleanup = _import_browser_panel()
        try:
            ctrl = _FakeController()
            panel = bp.GeoiPanel.__new__(bp.GeoiPanel)
            panel._c = ctrl
            panel._add_to_map("service", {"name": "s"})
            panel._add_to_map("tile", {"id": 1})
            panel._add_to_map("project", {"id": 2})
            panel._add_to_map("folder", {"id": "f"})  # no-op
            self.assertEqual(ctrl.calls, [
                ("service", {"name": "s"}),
                ("tile", {"id": 1}),
                ("project", {"id": 2}),
            ])
        finally:
            cleanup()


if __name__ == "__main__":
    unittest.main()
