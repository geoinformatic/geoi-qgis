"""Release 1.7.0 (#1001) — the content-browser UX overhaul, client half.

Covers the four client-side features:

* F1 — the Discover panel reads NO owner PII (``ownerName``); a discovered row
  is labelled by its ``created`` publication date instead.
* F2 — entering Discover shows ALL four categories immediately (empty-query
  fetch); typing filters; clearing restores the full page.
* F4 — ``add_service`` frames the map from the server's ``fullExtent`` (the
  ArcGIS FeatureServer provider's ``extent()`` is empty/world at add time).
* F5 — ``client.basemaps()`` parses the base-map endpoint and ``add_basemap``
  builds a valid native XYZ layer URI.
"""

import io
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import panel_stub  # noqa: E402

from geoi.geoi_client import GeoiClient  # noqa: E402


class _RecCtrl:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name,) + a)
        return rec


_DISCOVER = {
    "services": [{"name": "roads", "title": "Roads", "visibility": "public",
                  "ownerName": "Ada Lovelace", "created": "2026-07-01T09:00:00Z"}],
    "projects": [{"id": 1, "name": "Trip", "visibility": "public",
                  "ownerName": "ada@example.com", "created": "2026-07-02"}],
    "tiles": [{"id": 2, "title": "Ortho", "visibility": "public",
               "ownerName": "Ben", "created": 1751356800}],
    "tiles3d": [{"id": 3, "title": "Scan", "visibility": "public",
                 "ownerName": "Cy", "created": "2026-07-04T00:00:00Z"}],
}


# ------------------------------------------------------------------------- F1
class DiscoverNoOwnerPiiTest(unittest.TestCase):
    """No code path reads ``ownerName``; a discovered row shows a date."""

    def setUp(self):
        self.bp, self._cleanup = panel_stub.install()
        self.panel = self.bp.GeoiPanel(_RecCtrl())

    def tearDown(self):
        self._cleanup()

    def test_source_never_reads_ownername(self):
        import geoi.gui.browser_panel as bp_src
        import geoi.geoi_client as gc_src
        for mod in (bp_src, gc_src):
            with open(mod.__file__, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(
                "ownerName", src,
                "{} must not read owner PII (#1001)".format(mod.__name__))

    def test_discover_row_shows_publication_date_not_owner(self):
        self.panel.populate_discover(_DISCOVER, query="")
        seen = []
        for cat in self.panel._tree.top:
            leaf = cat.child(0)
            seen.append(leaf.text(1))
        # Every leaf's subtitle column is a YYYY-MM-DD date, and never an owner.
        for sub in seen:
            self.assertRegex(sub, r"^\d{4}-\d{2}-\d{2}$", "row must show a date")
            self.assertNotIn("Ada", sub)
            self.assertNotIn("@", sub)

    def test_pub_date_parses_epoch_and_iso_and_bad_values(self):
        pd = self.bp._pub_date
        self.assertEqual(pd({"created": "2026-07-01T09:00:00Z"}), "2026-07-01")
        self.assertEqual(pd({"created": "2026-07-02"}), "2026-07-02")
        self.assertEqual(pd({"created": 1751356800}), "2025-07-01")  # epoch s
        self.assertEqual(pd({"created": 1751356800000}), "2025-07-01")  # epoch ms
        self.assertEqual(pd({}), "")
        self.assertEqual(pd({"created": None}), "")
        self.assertEqual(pd({"created": True}), "")


# ------------------------------------------------------------------------- F2
class DiscoverShowsAllCategoriesTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = panel_stub.install()
        self.ctrl = _RecCtrl()
        self.panel = self.bp.GeoiPanel(self.ctrl)

    def tearDown(self):
        self._cleanup()

    def _titles(self):
        return [it.text(0) for it in self.panel._tree.top]

    def test_entering_discover_empty_fetches_all_categories(self):
        # Switching to Discover with an empty box dispatches an empty-query
        # fetch (the server's "recent public" page for every kind).
        self.ctrl.calls = []
        self.panel._set_mode("discover")
        self.assertIn(("search_discover", ""), self.ctrl.calls)
        # When the controller responds, all four buckets render.
        self.panel.populate_discover(_DISCOVER, query="")
        self.assertEqual(
            self._titles(),
            ["Feature Services", "Web Maps", "Tile Services",
             "3D Tiles Services"])

    def test_typing_filters_the_loaded_page_client_side(self):
        self.panel._set_mode("discover")
        self.panel.populate_discover(_DISCOVER, query="")
        self.panel._search.setText("ortho")
        self.panel._on_search_changed("ortho")
        self.assertEqual(self._titles(), ["Tile Services"])

    def test_clearing_restores_the_full_page_without_refetch(self):
        self.panel._set_mode("discover")
        self.panel.populate_discover(_DISCOVER, query="")  # caches the full page
        # Narrow, then clear the box.
        self.panel._search.setText("ortho")
        self.panel._on_search_changed("ortho")
        self.assertEqual(self._titles(), ["Tile Services"])
        self.ctrl.calls = []
        self.panel._search.setText("")
        self.panel._on_search_changed("")
        # All four categories are back — restored from the cache, NO refetch.
        self.assertEqual(
            self._titles(),
            ["Feature Services", "Web Maps", "Tile Services",
             "3D Tiles Services"])
        self.assertEqual(
            [c for c in self.ctrl.calls if c[0] == "search_discover"], [],
            "clearing restores from cache, it must not refetch")


# ----------------------------------------------------- shared bare-plugin stubs
def _bare_plugin():
    saved = {
        k: sys.modules.get(k)
        for k in ("qgis", "qgis.core", "qgis.PyQt", "qgis.PyQt.QtCore",
                  "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
                  "geoi.gui.browser_panel", "geoi.gui.dialogs", "geoi.auth",
                  "geoi.tasks", "geoi.plugin")
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


# ------------------------------------------------------------------------- F4
class ServiceExtentCoordsTest(unittest.TestCase):
    """The pure FeatureServer-extent extractor (F4)."""

    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def test_full_extent_with_wkid(self):
        info = {"fullExtent": {"xmin": 10, "ymin": 20, "xmax": 30, "ymax": 40,
                               "spatialReference": {"latestWkid": 4326}}}
        self.assertEqual(
            self.plugin_mod._service_extent_coords(info),
            (10.0, 20.0, 30.0, 40.0, "EPSG:4326"))

    def test_initial_extent_is_the_fallback(self):
        info = {"initialExtent": {"xmin": 1, "ymin": 2, "xmax": 3, "ymax": 4},
                "spatialReference": {"wkid": 102100, "latestWkid": 3857}}
        self.assertEqual(
            self.plugin_mod._service_extent_coords(info),
            (1.0, 2.0, 3.0, 4.0, "EPSG:3857"))

    def test_degenerate_and_absent_are_none(self):
        m = self.plugin_mod
        self.assertIsNone(m._service_extent_coords({}))
        self.assertIsNone(m._service_extent_coords(
            {"fullExtent": {"xmin": 5, "ymin": 5, "xmax": 5, "ymax": 9}}))
        self.assertIsNone(m._service_extent_coords(
            {"fullExtent": {"xmin": "x", "ymin": 2, "xmax": 3, "ymax": 4}}))


class FrameServiceExtentTest(unittest.TestCase):
    """``_frame_service_extent`` frames from the SERVER bounds even when the
    provider's ``extent()`` is still empty/world at add time (F4)."""

    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _install_qgis(self, captured):
        qc = sys.modules["qgis.core"]

        class _Rect:
            def __init__(self, w, s, e, n):
                self.coords = (w, s, e, n)

        class _CRS:
            def __init__(self, authid):
                self.authid = authid

        class _Xform:
            def __init__(self, src, dst, project):
                pass

            def transformBoundingBox(self, rect):
                w, s, e, n = rect.coords
                return _Rect(w * 2, s * 2, e * 2, n * 2)  # visible "reproject"

        qc.QgsRectangle = _Rect
        qc.QgsCoordinateReferenceSystem = _CRS
        qc.QgsCoordinateTransform = _Xform
        qc.QgsProject = types.SimpleNamespace(instance=lambda: None)

        class _Canvas:
            def mapSettings(self):
                return types.SimpleNamespace(
                    destinationCrs=lambda: _CRS("EPSG:3857"))

            def setExtent(self, rect):
                captured["extent"] = rect.coords

            def refresh(self):
                captured["refreshed"] = True

        return _Canvas()

    def _plugin(self, canvas):
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p.iface = types.SimpleNamespace(mapCanvas=lambda: canvas)
        return p

    def test_frames_from_server_full_extent(self):
        captured = {}
        canvas = self._install_qgis(captured)
        p = self._plugin(canvas)
        called = {"zoom": False}
        p._zoom_to_layers = lambda layers: called.__setitem__("zoom", True)
        info = {"fullExtent": {"xmin": 10, "ymin": 20, "xmax": 30, "ymax": 40,
                               "spatialReference": {"latestWkid": 4326}}}
        # A layer whose provider extent is still the whole world at add time.
        world_layer = types.SimpleNamespace(extent=lambda: "WORLD")
        p._frame_service_extent([world_layer], info)
        # The canvas was framed to the REPROJECTED server bounds, not the world.
        self.assertEqual(captured["extent"], (20.0, 40.0, 60.0, 80.0))
        self.assertFalse(called["zoom"],
                         "server extent present -> provider fallback unused")

    def test_falls_back_to_provider_when_no_server_extent(self):
        captured = {}
        canvas = self._install_qgis(captured)
        p = self._plugin(canvas)
        called = {"zoom": False}
        p._zoom_to_layers = lambda layers: called.__setitem__("zoom", True)
        p._frame_service_extent([object()], {})  # no fullExtent/initialExtent
        self.assertTrue(called["zoom"], "no server extent -> use provider")
        self.assertNotIn("extent", captured)


# ------------------------------------------------------------------------- F5
class _JsonResp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body.encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class BasemapsClientTest(unittest.TestCase):
    def _client(self, opener):
        return GeoiClient(base_url="https://geoi.de", opener=opener)

    def test_parses_the_endpoint(self):
        seen = {}

        class _Opener:
            def open(self, req, timeout=None):
                seen["url"] = req.full_url
                return _JsonResp(
                    '{"basemaps":[{"id":"osm","name":"OSM",'
                    '"url":"https://{s}.tile.osm.org/{z}/{x}/{y}.png",'
                    '"subdomains":"abc","maxNativeZoom":19,"type":"xyz"}]}')

        c = self._client(_Opener())
        rows = c.basemaps()
        self.assertEqual(seen["url"], "https://geoi.de/platform/basemaps")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "osm")

    def test_fail_soft_on_bad_shape(self):
        class _Opener:
            def open(self, req, timeout=None):
                return _JsonResp('{"nope":true}')

        self.assertEqual(self._client(_Opener()).basemaps(), [])


class BasemapUriTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def test_xyz_source_substitutes_subdomain_and_zmax(self):
        bm = {"url": "https://{s}.tile.osm.org/{z}/{x}/{y}.png",
              "subdomains": "abc", "maxNativeZoom": 19}
        src = self.plugin_mod._basemap_xyz_source(bm)
        self.assertTrue(src.startswith("type=xyz&url="))
        self.assertIn("&zmax=19", src)
        # The Leaflet-style "abc" subdomain list -> first char 'a' substituted;
        # the {s} placeholder must be gone (percent-encoded url).
        from urllib.parse import unquote
        self.assertIn("https://a.tile.osm.org/{z}/{x}/{y}.png", unquote(src))
        self.assertNotIn("{s}", unquote(src))

    def test_comma_separated_subdomains(self):
        bm = {"url": "https://{s}.x/{z}/{x}/{y}.png", "subdomains": "s1,s2,s3"}
        from urllib.parse import unquote
        self.assertIn("https://s1.x/", unquote(
            self.plugin_mod._basemap_xyz_source(bm)))

    def test_array_subdomains_uses_first_element(self):
        # The real /platform/basemaps endpoint sends subdomains as a JSON
        # ARRAY for the Bayern layers (["1","2","3"]) — a bare str() of a list
        # previously produced a garbage host like "['1'.bayernwolke.de".
        bm = {"url": "https://wmts{s}.bayernwolke.de/{z}/{x}/{y}.png",
              "subdomains": ["1", "2", "3"]}
        from urllib.parse import unquote
        src = unquote(self.plugin_mod._basemap_xyz_source(bm))
        self.assertIn("https://wmts1.bayernwolke.de/", src)
        self.assertNotIn("[", src)
        self.assertNotIn("{s}", src)

    def test_placeholder_with_no_subdomains_defaults_to_a(self):
        # osm/topo carry {s} in the URL but the endpoint sends NO subdomains
        # field for them — QGIS never expands a literal {s}, so it must be
        # substituted with a sane default rather than left in the URI.
        bm = {"url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"}
        from urllib.parse import unquote
        src = unquote(self.plugin_mod._basemap_xyz_source(bm))
        self.assertIn("https://a.tile.openstreetmap.org/", src)
        self.assertNotIn("{s}", src)

    def test_add_basemap_builds_valid_xyz_layer(self):
        captured = {}

        class _CapLayer:
            def __init__(self, source, name, provider):
                captured.update(source=source, name=name, provider=provider)

            def isValid(self):
                return True

        self.plugin_mod.QgsRasterLayer = _CapLayer
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._add_layer_to_toc = lambda layer, top: captured.__setitem__(
            "top", top)
        p._zoom_to_layers = lambda layers: None
        p._info = lambda *a: None
        p._warn = lambda *a: captured.__setitem__("warned", a)
        bm = {"id": "osm", "name": "OpenStreetMap",
              "url": "https://{s}.tile.osm.org/{z}/{x}/{y}.png",
              "subdomains": "abc", "maxNativeZoom": 19}
        p.add_basemap(bm)
        self.assertEqual(captured["provider"], "wms")
        self.assertEqual(captured["name"], "OpenStreetMap")
        self.assertEqual(captured["source"],
                         self.plugin_mod._basemap_xyz_source(bm))
        self.assertEqual(captured["top"], False, "base maps go to the bottom")
        self.assertNotIn("warned", captured)


if __name__ == "__main__":
    unittest.main()
