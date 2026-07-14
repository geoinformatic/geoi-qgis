"""WS2 — the declarative capability/action registry that replaced the four
per-kind if/elif branches in the content-browser context menu.

Pure: ``browser_panel.actions_for`` computes the ordered menu labels with no Qt,
so we import the REAL module with qgis/Qt stubbed (like ``test_bulk_share``) and
assert the menu contract directly. Three guarantees:

  (a) regression-lock — the registry yields the CURRENT action set for every
      kind (owned + shared), so no existing action was lost in the refactor;
  (b) an OWNED 3D-Tiles item now offers Rename / Share / Move / Copy-URL, and a
      SHARED one hides all owner-only actions;
  (c) a DISCOVER item offers exactly {Add to map, Copy URL, Open in geoi}.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


# The action set each kind produced BEFORE the registry refactor (the exact,
# ordered menu). The registry must still yield these — plus the new owner-only
# 3D-Tiles actions, which are asserted separately.
_OWNED = {
    "service": [
        "Add to map", "Rename", "Share…", "Editable (read/write)",
        "Move to folder…", "Copy service URL", "Open geoi web app",
        "Delete service",
    ],
    "project": [
        "Add to map", "Rename", "Share…", "Move to folder…",
        "Open geoi web app", "Delete project",
    ],
    "tile": [
        "Add as XYZ layer", "Add as WMTS layer", "Rename", "Share…",
        "Move to folder…", "Copy XYZ URL", "Copy WMTS URL", "Copy PMTiles URL",
        "Open in geoi web app", "Delete tile service",
    ],
    "folder": ["New subfolder…", "Rename", "Delete folder"],
}
_SHARED = {
    "tile": [
        "Add as XYZ layer", "Add as WMTS layer", "Copy XYZ URL",
        "Copy WMTS URL", "Copy PMTiles URL", "Open in geoi web app",
    ],
}


class RegressionLockTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = _import_browser_panel()

    def tearDown(self):
        self._cleanup()

    def test_owned_kinds_keep_their_full_menu(self):
        for kind, expected in _OWNED.items():
            payload = {"editable": True} if kind == "service" else {}
            self.assertEqual(
                self.bp.actions_for(kind, payload=payload), expected,
                "owned {} menu drifted".format(kind))

    def test_shared_tile_hides_owner_only(self):
        self.assertEqual(
            self.bp.actions_for("tile", shared=True), _SHARED["tile"])

    def test_no_current_tiles3d_action_was_lost(self):
        # The pre-refactor owned 3D-Tiles menu — every one must still appear.
        current = {
            "Add to map (QGIS 3.34+)", "Open deck.gl preview in web app",
            "Open Cesium preview in web app", "Copy tileset URL",
            "Delete 3D Tiles service",
        }
        labels = set(self.bp.actions_for("tiles3d"))
        self.assertTrue(current <= labels,
                        "lost 3D-Tiles actions: " + str(current - labels))


class Tiles3dParityTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = _import_browser_panel()

    def tearDown(self):
        self._cleanup()

    def test_owned_tiles3d_now_offers_owner_actions(self):
        labels = self.bp.actions_for("tiles3d")
        for wanted in ("Rename", "Share…", "Move to folder…",
                       "Copy tileset URL"):
            self.assertIn(wanted, labels)
        # Full ordered menu (new owner actions folded into the shared order).
        self.assertEqual(labels, [
            "Add to map (QGIS 3.34+)", "Rename", "Share…", "Move to folder…",
            "Copy tileset URL", "Open deck.gl preview in web app",
            "Open Cesium preview in web app", "Delete 3D Tiles service",
        ])

    def test_shared_tiles3d_hides_owner_actions(self):
        labels = self.bp.actions_for("tiles3d", shared=True)
        for hidden in ("Rename", "Share…", "Move to folder…",
                       "Delete 3D Tiles service"):
            self.assertNotIn(hidden, labels)
        self.assertEqual(labels, [
            "Add to map (QGIS 3.34+)", "Copy tileset URL",
            "Open deck.gl preview in web app",
            "Open Cesium preview in web app",
        ])


class DiscoverMenuTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = _import_browser_panel()

    def tearDown(self):
        self._cleanup()

    def test_every_discover_kind_offers_exactly_three(self):
        for kind in ("service", "project", "tile", "tiles3d"):
            self.assertEqual(
                self.bp.actions_for(kind, discover=True),
                ["Add to map", "Copy URL", "Open in geoi"],
                "discover {} menu wrong".format(kind))

    def test_discover_forces_not_owned(self):
        # discover=True implies shared regardless of the shared flag passed.
        self.assertEqual(
            self.bp.actions_for("tiles3d", shared=False, discover=True),
            ["Add to map", "Copy URL", "Open in geoi"])


if __name__ == "__main__":
    unittest.main()
