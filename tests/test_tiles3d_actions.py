"""3D Tiles Services wiring (round 7, Phase 2b) — plugin-side units.

Covers, all pure / stdlib with ``qgis`` stubbed (mirroring
``test_tile_actions``):

* ``preview3d_deeplink_url`` — ``<base>/?preview3d=<id>``, ``&ptoken=`` ONLY
  for a non-public service, URL-encoded;
* ``tiles3d_layer_uri`` — the pure ``url=…`` QgsTiledSceneLayer datasource
  builder, with and without a share token on the URL;
* the QGIS 3.34+ version gate — an old QGIS gets the message-bar pointer and
  the client is never touched;
* ``_Tiles3dAddRouter`` — the proxy that reroutes the encoder's
  ``tiles3d_create`` onto ``tiles3d_add(service_id, …)``;
* ``GeoiClient.tiles3d_add`` — multipart POST to
  ``/tiles3d/services/<id>/add`` (file part ``archive``, optional ``bounds``
  form field, bearer attached) against a stubbed transport;
* ``_las_header_epsg`` — the LAS/LAZ header CRS peek (GeoKey directory and
  WKT VLR variants), and the multi-select publish routing.

These tests intentionally import NONE of the sibling wave's new symbols
(``pointcloud`` / ``tiles3d_encoder`` / ``Tiles3dPointCloudPublishTask``) —
the URL builders / routing / client lanes are provable in isolation.
"""

import io
import json
import os
import struct
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bare_plugin():
    """Import ``geoi.plugin`` with qgis + Qt stubbed — same convention as
    ``test_tile_actions._bare_plugin`` — returning ``(module, cleanup)``."""
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
                              "ShareDialog", "ManageGroupsDialog"]),
        ("geoi.auth", ["SessionStore"]),
        ("geoi.tasks", ["ActionTask", "BasemapsTask", "CatalogTask", "DiscoverTask", "PublishTask",
                        "RasterPublishTask", "SaveProjectTask", "SignInTask",
                        "Tiles3dPublishTask"]),
    ):
        m = types.ModuleType(name)
        for n in names:
            setattr(m, n, _Any)
        sys.modules[name] = m
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


class _FakeTiles3dClient:
    """A client stub with a scripted 3D-Tiles detail. ``tile_url`` delegates
    to the REAL pure GeoiClient implementation so tokenization behaviour is
    the production one."""

    def __init__(self, base_url, detail):
        self.base_url = base_url
        self.token = "T"
        self._detail = detail
        self.calls = []

    def tiles3d_get(self, service_id):
        self.calls.append(("get", service_id))
        return dict(self._detail, id=self._detail.get("id", service_id))

    def tile_url(self, raw_url, share_token=None, visibility=None):
        from geoi.geoi_client import GeoiClient

        c = GeoiClient.__new__(GeoiClient)  # no network in __init__ path
        c.base_url = self.base_url
        return GeoiClient.tile_url(
            c, raw_url, share_token=share_token, visibility=visibility)


# ------------------------------------------------------- preview3d deep link
class Preview3dDeepLinkTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _plugin(self, base_url, detail, warned):
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._client = _FakeTiles3dClient(base_url, detail)
        p._warn = lambda *a: warned.append(a)
        return p

    def test_public_service_omits_ptoken(self):
        warned = []
        p = self._plugin(
            "https://www.geoi.de",
            {"visibility": "public", "shareToken": "ignored"}, warned)
        url, err = p.preview3d_deeplink_url({"id": 42})
        self.assertIsNone(err)
        self.assertEqual(url, "https://www.geoi.de/?preview3d=42")
        self.assertNotIn("ptoken", url)
        self.assertEqual(warned, [])

    def test_private_service_appends_encoded_ptoken(self):
        warned = []
        p = self._plugin(
            "https://staging.geoi.de",
            {"visibility": "private", "shareToken": "tok/with?chars"}, warned)
        url, err = p.preview3d_deeplink_url({"id": "7"})
        self.assertIsNone(err)
        self.assertTrue(url.startswith("https://staging.geoi.de/?preview3d=7"))
        self.assertIn("&ptoken=tok%2Fwith%3Fchars", url)

    def test_groups_visibility_also_gets_ptoken(self):
        p = self._plugin(
            "https://www.geoi.de",
            {"visibility": "groups", "shareToken": "TKN"}, [])
        url, _err = p.preview3d_deeplink_url({"id": 3})
        self.assertIn("&ptoken=TKN", url)

    def test_no_id_is_an_error(self):
        p = self._plugin("https://www.geoi.de", {}, [])
        url, err = p.preview3d_deeplink_url({})
        self.assertEqual(url, "")
        self.assertTrue(err)

    def test_default_engine_omits_the_engine_param(self):
        # Backward compat: an existing deck-only link stays byte-identical.
        p = self._plugin(
            "https://www.geoi.de", {"visibility": "public"}, [])
        url, err = p.preview3d_deeplink_url({"id": 42})
        self.assertIsNone(err)
        self.assertEqual(url, "https://www.geoi.de/?preview3d=42")
        self.assertNotIn("engine", url)

    def test_explicit_deck_engine_still_omits_the_param(self):
        p = self._plugin(
            "https://www.geoi.de", {"visibility": "public"}, [])
        url, _err = p.preview3d_deeplink_url({"id": 42}, engine="deck")
        self.assertEqual(url, "https://www.geoi.de/?preview3d=42")

    def test_cesium_engine_appends_the_param(self):
        p = self._plugin(
            "https://www.geoi.de", {"visibility": "public"}, [])
        url, err = p.preview3d_deeplink_url({"id": 42}, engine="cesium")
        self.assertIsNone(err)
        self.assertEqual(
            url, "https://www.geoi.de/?preview3d=42&engine=cesium")

    def test_cesium_engine_rides_alongside_the_ptoken(self):
        p = self._plugin(
            "https://staging.geoi.de",
            {"visibility": "private", "shareToken": "TKN"}, [])
        url, _err = p.preview3d_deeplink_url({"id": 7}, engine="cesium")
        self.assertIn("&ptoken=TKN", url)
        self.assertIn("&engine=cesium", url)

    def test_unknown_engine_falls_back_to_deck(self):
        p = self._plugin(
            "https://www.geoi.de", {"visibility": "public"}, [])
        url, _err = p.preview3d_deeplink_url({"id": 42}, engine="potato")
        self.assertEqual(url, "https://www.geoi.de/?preview3d=42")

    def _stub_desktop_services(self):
        """Give the stubbed Qt modules the two symbols
        ``open_tiles3d_preview_in_web_app`` imports, recording the opened URL."""
        opened = []
        sys.modules["qgis.PyQt.QtCore"].QUrl = lambda u: u
        sys.modules["qgis.PyQt.QtGui"].QDesktopServices = types.SimpleNamespace(
            openUrl=lambda u: opened.append(u))
        return opened

    def test_open_in_web_app_deck_default_opens_deck_url_and_names_it(self):
        opened = self._stub_desktop_services()
        p = self._plugin(
            "https://www.geoi.de", {"visibility": "public"}, [])
        infos = []
        p._info = lambda msg: infos.append(msg)
        p.open_tiles3d_preview_in_web_app({"id": 42})
        self.assertEqual(opened, ["https://www.geoi.de/?preview3d=42"])
        self.assertEqual(len(infos), 1)
        self.assertIn("deck.gl", infos[0])

    def test_open_in_web_app_forwards_cesium_and_names_it(self):
        opened = self._stub_desktop_services()
        p = self._plugin(
            "https://www.geoi.de", {"visibility": "public"}, [])
        infos = []
        p._info = lambda msg: infos.append(msg)
        p.open_tiles3d_preview_in_web_app({"id": 42}, engine="cesium")
        self.assertEqual(
            opened, ["https://www.geoi.de/?preview3d=42&engine=cesium"])
        self.assertEqual(len(infos), 1)
        self.assertIn("Cesium", infos[0])


# ----------------------------------------------- tileset share URL + copy
class TilesetShareUrlTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _plugin(self, detail):
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._client = _FakeTiles3dClient("https://www.geoi.de", detail)
        return p

    def test_private_url_is_absolutized_and_tokenized(self):
        p = self._plugin({
            "visibility": "private", "shareToken": "SEKRET",
            "urls": {"tileset": "/platform/tiles3d/services/5/tileset.json"},
        })
        url, err = p._tiles3d_tileset_share_url({"id": 5})
        self.assertIsNone(err)
        self.assertEqual(
            url,
            "https://www.geoi.de/platform/tiles3d/services/5/tileset.json"
            "?token=SEKRET")

    def test_public_url_has_no_token(self):
        p = self._plugin({
            "visibility": "public", "shareToken": "SEKRET",
            "urls": {"tileset": "/platform/tiles3d/services/5/tileset.json"},
        })
        url, err = p._tiles3d_tileset_share_url({"id": 5})
        self.assertIsNone(err)
        self.assertNotIn("token=", url)

    def test_missing_tileset_url_is_an_error(self):
        p = self._plugin({"visibility": "public", "urls": {}})
        url, err = p._tiles3d_tileset_share_url({"id": 5})
        self.assertEqual(url, "")
        self.assertTrue(err)


# ------------------------------------------------------- provider URI builder
class Tiles3dLayerUriTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def test_plain_url(self):
        self.assertEqual(
            self.plugin_mod.tiles3d_layer_uri(
                "https://geoi.de/platform/tiles3d/services/5/tileset.json"),
            "url=https://geoi.de/platform/tiles3d/services/5/tileset.json")

    def test_tokenized_url_rides_untouched(self):
        # The token query stays part of the url= value (the provider passes
        # the URL through to its HTTP fetches).
        self.assertEqual(
            self.plugin_mod.tiles3d_layer_uri(
                "https://x.test/t/tileset.json?token=SEKRET"),
            "url=https://x.test/t/tileset.json?token=SEKRET")

    def test_empty_is_still_well_formed(self):
        self.assertEqual(self.plugin_mod.tiles3d_layer_uri(""), "url=")
        self.assertEqual(self.plugin_mod.tiles3d_layer_uri(None), "url=")


# ------------------------------------------------------------- version gate
class VersionGateTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def test_no_qgis_version_reports_unsupported(self):
        # The stubbed qgis.core has no Qgis at all -> gate is False.
        self.assertFalse(self.plugin_mod._tiled_scene_support())

    def test_old_qgis_reports_unsupported(self):
        sys.modules["qgis.core"].Qgis = types.SimpleNamespace(
            QGIS_VERSION_INT=33200)
        sys.modules["qgis.core"].QgsTiledSceneLayer = object
        self.assertFalse(self.plugin_mod._tiled_scene_support())

    def test_qgis_334_with_class_reports_supported(self):
        sys.modules["qgis.core"].Qgis = types.SimpleNamespace(
            QGIS_VERSION_INT=33400)
        sys.modules["qgis.core"].QgsTiledSceneLayer = object
        self.assertTrue(self.plugin_mod._tiled_scene_support())

    def test_new_qgis_without_class_reports_unsupported(self):
        sys.modules["qgis.core"].Qgis = types.SimpleNamespace(
            QGIS_VERSION_INT=34000)
        self.assertFalse(self.plugin_mod._tiled_scene_support())

    def test_add_on_old_qgis_warns_and_never_touches_the_client(self):
        sys.modules["qgis.core"].Qgis = types.SimpleNamespace(
            QGIS_VERSION_INT=32800)
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)

        class _Untouchable:
            def __getattr__(self, name):
                raise AssertionError("client must not be called on old QGIS")

        p._client = _Untouchable()
        bar = []
        p._warn_bar = bar.append
        p.add_tiles3d_layer({"id": 1, "title": "Scan"})
        self.assertEqual(len(bar), 1)
        self.assertIn("3.34", bar[0])
        self.assertIn("deck.gl", bar[0])


# ---------------------------------------------------------- add router proxy
class Tiles3dAddRouterTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _client(self):
        class _Client:
            base_url = "https://www.geoi.de"

            def __init__(self):
                self.adds = []

            def tiles3d_add(self, service_id, data, filename="tileset.zip",
                            bounds=None, timeout=None):
                self.adds.append((service_id, data, filename, bounds))
                return {"id": service_id, "datasetCount": 2,
                        "urls": {"tileset": "/x/tileset.json"}}

            def tiles3d_tileset_url(self, raw):
                return "abs:" + (raw or "")

        return _Client()

    def test_create_bytes_route_to_add(self):
        client = self._client()
        router = self.plugin_mod._Tiles3dAddRouter(client, 5)
        svc = router.tiles3d_create(b"ZIPBYTES", title="ignored",
                                    filename="cloud.zip")
        self.assertEqual(client.adds, [(5, b"ZIPBYTES", "cloud.zip", None)])
        self.assertEqual(svc["id"], 5)

    def test_create_forwards_bounds_to_add(self):
        # The encoder feature-detects a `bounds` kwarg on tiles3d_create and
        # rides the cloud's WGS84 bounds through it — the router must both
        # EXPOSE the kwarg (so the detection passes) and forward it.
        client = self._client()
        router = self.plugin_mod._Tiles3dAddRouter(client, 5)
        import inspect

        self.assertIn("bounds",
                      inspect.signature(router.tiles3d_create).parameters)
        router.tiles3d_create(b"Z", filename="a.zip",
                              bounds=[10.0, 50.0, 10.1, 50.1])
        self.assertEqual(client.adds[0][3], [10.0, 50.0, 10.1, 50.1])

    def test_create_path_route_reads_the_file(self):
        import tempfile

        client = self._client()
        router = self.plugin_mod._Tiles3dAddRouter(client, 9)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
            fh.write(b"PKDATA")
            path = fh.name
        try:
            router.tiles3d_create(path, title="x")
        finally:
            os.unlink(path)
        sid, data, name, _bounds = client.adds[0]
        self.assertEqual((sid, data), (9, b"PKDATA"))
        self.assertEqual(name, os.path.basename(path))

    def test_everything_else_delegates(self):
        client = self._client()
        router = self.plugin_mod._Tiles3dAddRouter(client, 5)
        self.assertEqual(router.base_url, "https://www.geoi.de")
        self.assertEqual(router.tiles3d_tileset_url("/t.json"), "abs:/t.json")


# --------------------------------------------------- multi-select routing
class PublishRoutingTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _plugin(self, files):
        sys.modules["qgis.PyQt.QtWidgets"].QFileDialog = types.SimpleNamespace(
            getOpenFileNames=lambda *a, **k: (list(files), ""))
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._client = types.SimpleNamespace(token="T")
        p.iface = types.SimpleNamespace(mainWindow=lambda: None)
        p._warnings = []
        p._warn = lambda *a: p._warnings.append(a)
        p._zip_calls = []
        p._publish_tiles3d_zip = p._zip_calls.append
        p._cloud_calls = []
        p._publish_point_clouds = p._cloud_calls.append
        return p

    def test_single_zip_goes_to_the_prepared_lane(self):
        p = self._plugin(["/data/scan.ZIP"])
        p.publish_tiles3d()
        self.assertEqual(p._zip_calls, ["/data/scan.ZIP"])
        self.assertEqual(p._cloud_calls, [])
        self.assertEqual(p._warnings, [])

    def test_point_clouds_go_to_the_cloud_lane(self):
        p = self._plugin(["/data/a.las", "/data/b.LAZ", "/data/c.ply"])
        p.publish_tiles3d()
        self.assertEqual(p._cloud_calls,
                         [["/data/a.las", "/data/b.LAZ", "/data/c.ply"]])
        self.assertEqual(p._zip_calls, [])

    def test_mixed_zip_and_cloud_warns(self):
        p = self._plugin(["/data/a.las", "/data/scan.zip"])
        p.publish_tiles3d()
        self.assertEqual(p._zip_calls, [])
        self.assertEqual(p._cloud_calls, [])
        self.assertEqual(len(p._warnings), 1)

    def test_two_zips_warn(self):
        p = self._plugin(["/a.zip", "/b.zip"])
        p.publish_tiles3d()
        self.assertEqual(p._zip_calls, [])
        self.assertEqual(len(p._warnings), 1)

    def test_cancel_selects_nothing(self):
        p = self._plugin([])
        p.publish_tiles3d()
        self.assertEqual(p._zip_calls, [])
        self.assertEqual(p._cloud_calls, [])
        self.assertEqual(p._warnings, [])


# ----------------------------------------------------------- reproject_fn
class MakeReprojectFnTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def test_none_or_missing_qgis_gives_none(self):
        self.assertIsNone(self.plugin_mod._make_reproject_fn(None))
        # The stubbed qgis.core lacks the transform classes -> None (the
        # encoder's built-in placement path takes over).
        self.assertIsNone(self.plugin_mod._make_reproject_fn("EPSG:25832"))

    def _install_transform_stubs(self):
        qc = sys.modules["qgis.core"]

        class _Pt:
            def __init__(self, x, y):
                self._x, self._y = x, y

            def x(self):
                return self._x

            def y(self):
                return self._y

        class _CRS:
            def __init__(self, authid):
                self._authid = str(authid)

            def isValid(self):
                return self._authid.startswith("EPSG:")

        class _Xform:
            def __init__(self, src, dst, project):
                pass

            def transform(self, pt):
                # pretend-project: lon = x+100, lat = y+200
                return _Pt(pt.x() + 100, pt.y() + 200)

        qc.QgsCoordinateReferenceSystem = _CRS
        qc.QgsCoordinateTransform = _Xform
        qc.QgsPointXY = _Pt
        qc.QgsProject = types.SimpleNamespace(instance=lambda: None)

    def test_valid_crs_yields_lat_lon_order(self):
        self._install_transform_stubs()
        fn = self.plugin_mod._make_reproject_fn("EPSG:25832")
        self.assertIsNotNone(fn)
        # transform gives (lon=x+100, lat=y+200); the wrapper returns
        # (lat, lon) — i.e. (pt.y(), pt.x()).
        self.assertEqual(fn(1, 2), (202, 101))

    def test_invalid_crs_gives_none(self):
        self._install_transform_stubs()
        self.assertIsNone(self.plugin_mod._make_reproject_fn("bogus"))


# ------------------------------------------------------------ LAS CRS peek
def _las_bytes(vlrs):
    """A minimal synthetic LAS 1.2 file: 375-byte header + the given VLRs
    (each ``(user_id, record_id, payload)``)."""
    header = bytearray(375)
    header[0:4] = b"LASF"
    struct.pack_into("<H", header, 94, 375)        # header size
    struct.pack_into("<I", header, 100, len(vlrs))  # number of VLRs
    out = bytes(header)
    for user_id, record_id, payload in vlrs:
        vlr = bytearray(54)
        vlr[2:2 + len(user_id)] = user_id
        struct.pack_into("<H", vlr, 18, record_id)
        struct.pack_into("<H", vlr, 20, len(payload))
        out += bytes(vlr) + payload
    return out


def _geokey_payload(entries):
    """A GeoKeyDirectory record: header + ``(key, location, count, value)``."""
    payload = struct.pack("<4H", 1, 1, 0, len(entries))
    for entry in entries:
        payload += struct.pack("<4H", *entry)
    return payload


class LasHeaderEpsgTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()
        import tempfile

        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)
        self._cleanup()

    def _write(self, name, data):
        path = os.path.join(self._tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_projected_geokey_wins(self):
        payload = _geokey_payload([
            (2048, 0, 1, 4258),   # geographic
            (3072, 0, 1, 25832),  # projected — wins
        ])
        path = self._write("a.las", _las_bytes(
            [(b"LASF_Projection", 34735, payload)]))
        self.assertEqual(self.plugin_mod._las_header_epsg(path), 25832)

    def test_geographic_geokey_when_no_projected(self):
        payload = _geokey_payload([(2048, 0, 1, 4326)])
        path = self._write("b.las", _las_bytes(
            [(b"LASF_Projection", 34735, payload)]))
        self.assertEqual(self.plugin_mod._las_header_epsg(path), 4326)

    def test_wkt_vlr_yields_epsg(self):
        wkt = b'PROJCRS["ETRS89 / UTM 32N",ID["EPSG",25833]]'
        path = self._write("c.laz", _las_bytes(
            [(b"LASF_Projection", 2112, wkt)]))
        self.assertEqual(self.plugin_mod._las_header_epsg(path), 25833)

    def test_no_projection_vlr_is_none(self):
        path = self._write("d.las", _las_bytes(
            [(b"SomeVendor", 999, b"junk")]))
        self.assertIsNone(self.plugin_mod._las_header_epsg(path))

    def test_non_las_extension_and_garbage_are_none(self):
        ply = self._write("e.ply", b"ply\nformat ascii 1.0\n")
        self.assertIsNone(self.plugin_mod._las_header_epsg(ply))
        junk = self._write("f.las", b"NOTALAS")
        self.assertIsNone(self.plugin_mod._las_header_epsg(junk))
        self.assertIsNone(self.plugin_mod._las_header_epsg(
            os.path.join(self._tmp, "missing.las")))


# --------------------------------------------------- client tiles3d_add lane
class _FakeResponse(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(payload if isinstance(payload, bytes) else payload.encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Records each urllib Request and returns queued (payload, status)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        payload, status = self._responses.pop(0)
        if status >= 400:
            import urllib.error

            err = urllib.error.HTTPError(req.full_url, status, "err", {}, None)
            err.read = lambda: payload if isinstance(payload, bytes) else payload.encode()
            raise err
        return _FakeResponse(payload, status)


class Tiles3dAddClientTest(unittest.TestCase):
    """``tiles3d_add`` mirrors ``tiles3d_create``'s multipart contract on the
    ``/tiles3d/services/<id>/add`` route (api/tiles3d.php reads the SAME
    ``archive`` field in both lanes; ``bounds`` is a plain form field)."""

    def _client(self, responses):
        from geoi.geoi_client import GeoiClient

        return GeoiClient(base_url="https://geoi.de",
                          opener=_FakeOpener(responses))

    def test_add_posts_multipart_archive_with_bearer(self):
        c = self._client([(json.dumps({"ok": True, "service": {
            "id": 5, "title": "Scan", "datasetCount": 2,
            "urls": {"tileset": "/platform/tiles3d/services/5/tileset.json"},
        }}), 200)])
        c.set_token("SESS-TOKEN")
        svc = c.tiles3d_add(5, b"PK\x03\x04ZIPBYTES", filename="cloud.zip")
        self.assertEqual(svc["datasetCount"], 2)

        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/platform/tiles3d/services/5/add"))
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertTrue(headers["content-type"].startswith(
            "multipart/form-data; boundary="))
        self.assertEqual(headers["authorization"], "Bearer SESS-TOKEN")
        self.assertIn(b'name="archive"; filename="cloud.zip"', req.data)
        self.assertIn(b"ZIPBYTES", req.data)
        # No bounds passed -> no bounds field in the body.
        self.assertNotIn(b'name="bounds"', req.data)

    def test_add_sends_bounds_as_plain_json_form_field(self):
        c = self._client([(json.dumps({"ok": True, "service": {
            "id": 7, "urls": {"tileset": "/x/7/tileset.json"}}}), 200)])
        c.set_token("T")
        c.tiles3d_add(7, b"ZIP", bounds=[10.0, 50.0, 10.1, 50.1])
        data = c._opener.requests[0].data
        self.assertIn(b'name="bounds"', data)
        self.assertIn(b"[10.0, 50.0, 10.1, 50.1]", data)
        # A plain form field (lands in $_POST): no filename on the part.
        head = data.split(b'name="bounds"', 1)[0]
        self.assertNotIn(b'name="bounds"; filename', data)
        self.assertTrue(head)  # smoke: multipart preamble exists

    def test_add_reads_a_zip_from_a_path(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
            fh.write(b"PKPATHDATA")
            path = fh.name
        try:
            c = self._client([(json.dumps({"ok": True, "service": {
                "id": 3, "urls": {"tileset": "/x/3/tileset.json"}}}), 200)])
            c.set_token("T")
            # The contracted default filename is "tileset.zip" — it applies
            # to a path input too unless a filename is passed explicitly.
            c.tiles3d_add(3, path)
            data = c._opener.requests[0].data
            self.assertIn(b"PKPATHDATA", data)
            self.assertIn(b'name="archive"; filename="tileset.zip"', data)
        finally:
            os.unlink(path)

    def test_add_2xx_without_service_raises(self):
        from geoi.geoi_client import GeoiError

        c = self._client([(json.dumps({"ok": True}), 200)])
        c.set_token("T")
        with self.assertRaises(GeoiError):
            c.tiles3d_add(5, b"ZIP")

    def test_create_accepts_and_sends_bounds_form_field(self):
        # The point-cloud lane feature-detects a `bounds` kwarg on
        # tiles3d_create (inspect.signature) — the client must EXPOSE it and
        # send it as a plain JSON form field, like the add lane.
        import inspect

        from geoi.geoi_client import GeoiClient

        self.assertIn(
            "bounds",
            inspect.signature(GeoiClient.tiles3d_create).parameters)
        c = self._client([(json.dumps({"ok": True, "service": {
            "id": 2, "urls": {"tileset": "/x/2/tileset.json"}}}), 200)])
        c.set_token("T")
        c.tiles3d_create(b"ZIP", title="t", bounds=[1.0, 2.0, 3.0, 4.0])
        data = c._opener.requests[0].data
        self.assertIn(b'name="bounds"', data)
        self.assertIn(b"[1.0, 2.0, 3.0, 4.0]", data)
        self.assertNotIn(b'name="bounds"; filename', data)

    def test_add_quota_exceeded_maps_to_friendly_text(self):
        from geoi import geoi_client
        from geoi.geoi_client import GeoiError

        c = self._client([(json.dumps({"ok": False, "error": "quota_exceeded",
                                       "message": "quota full"}), 413)])
        c.set_token("T")
        with self.assertRaises(GeoiError) as ctx:
            c.tiles3d_add(5, b"ZIP")
        self.assertEqual(ctx.exception.code, "quota_exceeded")
        self.assertIn(
            "quota",
            geoi_client.tiles3d_friendly_error(ctx.exception).lower())

    def test_rename_tiles3d_posts_title(self):
        # WS2 parity: rename POSTs {title} to /tiles3d/services/<id>.
        c = self._client([(json.dumps({"ok": True, "service": {
            "id": 3, "title": "New name"}}), 200)])
        c.set_token("T")
        svc = c.rename_tiles3d(3, "New name")
        self.assertEqual(svc["title"], "New name")
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/platform/tiles3d/services/3"))
        self.assertEqual(json.loads(req.data.decode()), {"title": "New name"})

    def test_move_tiles3d_posts_folder_id(self):
        # WS2 parity: move POSTs {folderId} to /tiles3d/services/<id>.
        c = self._client([(json.dumps({"ok": True, "service": {
            "id": 3, "folderId": "f7"}}), 200)])
        c.set_token("T")
        svc = c.move_tiles3d(3, "f7")
        self.assertEqual(svc["folderId"], "f7")
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/platform/tiles3d/services/3"))
        self.assertEqual(json.loads(req.data.decode()), {"folderId": "f7"})


# --------------------------------------------------- point-cloud tileset sniff
def _glb(mode="omit"):
    """A minimal but real-shaped GLB: a 12-byte header + a JSON chunk that
    describes one mesh/primitive. ``mode`` is the glTF primitive mode
    (0 = POINTS); pass ``"omit"`` for a primitive with NO mode key."""
    prim = {} if mode == "omit" else {"mode": mode}
    gltf = {"asset": {"version": "2.0"}, "meshes": [{"primitives": [prim]}]}
    body = json.dumps(gltf).encode("utf-8")
    body += b" " * ((4 - len(body) % 4) % 4)
    header = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(body))
    return header + struct.pack("<I", len(body)) + b"JSON" + body


class ContentUrisTest(unittest.TestCase):
    def test_root_only(self):
        from geoi import tiles3d

        self.assertEqual(
            tiles3d.content_uris({"root": {"content": {"uri": "0.glb"}}}),
            ["0.glb"])

    def test_root_and_children(self):
        from geoi import tiles3d

        ts = {"root": {"content": {"uri": "0.glb"}, "children": [
            {"content": {"uri": "1.glb"}},
            {"content": {"uri": "2.glb"}},
            {"no": "content"},  # tolerated, skipped
        ]}}
        self.assertEqual(tiles3d.content_uris(ts), ["0.glb", "1.glb", "2.glb"])

    def test_malformed_and_empty(self):
        from geoi import tiles3d

        self.assertEqual(tiles3d.content_uris({}), [])
        self.assertEqual(tiles3d.content_uris(None), [])
        self.assertEqual(tiles3d.content_uris({"root": 5}), [])
        self.assertEqual(tiles3d.content_uris({"root": {"content": {}}}), [])
        self.assertEqual(
            tiles3d.content_uris({"root": {"children": "nope"}}), [])


class GlbIsPointsTest(unittest.TestCase):
    def test_points_mode_is_true(self):
        from geoi import tiles3d

        self.assertTrue(tiles3d.glb_is_points(_glb(0)))

    def test_triangle_mode_is_false(self):
        from geoi import tiles3d

        self.assertFalse(tiles3d.glb_is_points(_glb(4)))

    def test_missing_mode_is_false(self):
        from geoi import tiles3d

        self.assertFalse(tiles3d.glb_is_points(_glb("omit")))

    def test_truncated_is_false_no_raise(self):
        from geoi import tiles3d

        self.assertFalse(tiles3d.glb_is_points(_glb(0)[:15]))

    def test_garbage_is_false(self):
        from geoi import tiles3d

        self.assertFalse(tiles3d.glb_is_points(b"NOT-A-GLB-AT-ALL-1234"))

    def test_empty_and_none_are_false(self):
        from geoi import tiles3d

        self.assertFalse(tiles3d.glb_is_points(b""))
        self.assertFalse(tiles3d.glb_is_points(None))

    def test_chunk_len_overrunning_the_buffer_is_false(self):
        from geoi import tiles3d

        bogus = (b"glTF" + struct.pack("<II", 2, 40)
                 + struct.pack("<I", 999) + b"JSON" + b"{}")
        self.assertFalse(tiles3d.glb_is_points(bogus))


class TilesetIsPointCloudTest(unittest.TestCase):
    def test_pnts_first_uri_is_true_without_fetch(self):
        from geoi import tiles3d

        calls = []
        ts = {"root": {"content": {"uri": "0.PNTS"}}}  # case-insensitive
        self.assertTrue(tiles3d.tileset_is_point_cloud(
            ts, lambda u: calls.append(u) or b""))
        self.assertEqual(calls, [])  # never fetched

    def test_glb_points_bytes_is_true(self):
        from geoi import tiles3d

        ts = {"root": {"content": {"uri": "0.glb"}}}
        self.assertTrue(tiles3d.tileset_is_point_cloud(ts, lambda u: _glb(0)))

    def test_glb_mesh_bytes_is_false(self):
        from geoi import tiles3d

        ts = {"root": {"content": {"uri": "0.glb"}}}
        self.assertFalse(tiles3d.tileset_is_point_cloud(ts, lambda u: _glb(4)))

    def test_fetch_error_fails_open_false(self):
        from geoi import tiles3d

        def boom(_u):
            raise RuntimeError("network down")

        ts = {"root": {"content": {"uri": "0.glb"}}}
        self.assertFalse(tiles3d.tileset_is_point_cloud(ts, boom))

    def test_empty_tileset_is_false(self):
        from geoi import tiles3d

        self.assertFalse(tiles3d.tileset_is_point_cloud({}, lambda u: b""))

    def test_other_extension_is_false(self):
        from geoi import tiles3d

        ts = {"root": {"content": {"uri": "0.b3dm"}}}
        self.assertFalse(tiles3d.tileset_is_point_cloud(ts, lambda u: b""))


# ------------------------------------------------ tileset content-url resolver
class TilesetContentUrlTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def test_relative_uri_resolved_against_tileset_path(self):
        f = self.plugin_mod._tileset_content_url
        self.assertEqual(
            f("https://www.geoi.de/platform/tiles3d/services/5/tileset.json",
              "0.glb"),
            "https://www.geoi.de/platform/tiles3d/services/5/0.glb")

    def test_share_token_query_is_carried_over(self):
        f = self.plugin_mod._tileset_content_url
        self.assertEqual(
            f("https://www.geoi.de/platform/tiles3d/services/5/tileset.json"
              "?token=SEKRET", "0.glb"),
            "https://www.geoi.de/platform/tiles3d/services/5/0.glb"
            "?token=SEKRET")

    def test_no_double_query_separator(self):
        f = self.plugin_mod._tileset_content_url
        out = f("https://x.test/t/tileset.json?token=T", "sub/9.glb?v=2")
        self.assertEqual(out.count("?"), 1)  # exactly one '?'
        self.assertIn("v=2", out)
        self.assertIn("token=T", out)
        self.assertTrue(out.startswith("https://x.test/t/sub/9.glb?"))


# -------------------------------------------------- add-layer point-cloud gate
class _SniffClient(_FakeTiles3dClient):
    """A tiles3d client stub that also scripts the point-cloud sniff calls."""

    def __init__(self, base_url, detail, tileset, glb=b"", raise_json=False):
        super().__init__(base_url, detail)
        self._tileset = tileset
        self._glb = glb
        self._raise_json = raise_json
        self.fetched = []

    def tiles3d_tileset_json(self, url, timeout=None):
        if self._raise_json:
            raise RuntimeError("boom")
        return self._tileset

    def fetch_bytes(self, url, timeout=None, max_bytes=None, auth=True):
        self.fetched.append(url)
        return self._glb


class _FakeSceneLayer:
    """Stand-in for QgsTiledSceneLayer that records each construction."""

    instances = []

    def __init__(self, *args):
        self.args = args
        _FakeSceneLayer.instances.append(self)

    def isValid(self):
        return True


class AddTiles3dPointCloudGateTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()
        _FakeSceneLayer.instances = []
        # Report a QGIS that CAN host a scene layer, so the version gate passes
        # and the point-cloud sniff runs.
        sys.modules["qgis.core"].Qgis = types.SimpleNamespace(
            QGIS_VERSION_INT=34000)
        sys.modules["qgis.core"].QgsTiledSceneLayer = _FakeSceneLayer

    def tearDown(self):
        self._cleanup()

    def _plugin(self, tileset, glb=b"", raise_json=False):
        detail = {"visibility": "public",
                  "urls": {"tileset":
                           "/platform/tiles3d/services/5/tileset.json"}}
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._client = _SniffClient("https://www.geoi.de", detail, tileset, glb,
                                 raise_json=raise_json)
        p.bars = []
        p._warn_bar = p.bars.append
        p.warns = []
        p._warn = lambda *a: p.warns.append(a)
        p.infos = []
        p._info = p.infos.append
        p.added = []
        p._add_layer_to_toc = lambda layer, top=False: p.added.append(layer)
        p.zoomed = []
        p._zoom_to_layers = p.zoomed.append
        return p

    def test_point_cloud_glb_warns_and_never_adds(self):
        p = self._plugin({"root": {"content": {"uri": "0.glb"}}}, glb=_glb(0))
        p.add_tiles3d_layer({"id": 5, "title": "Scan"})
        self.assertEqual(len(p.bars), 1)
        msg = p.bars[0]
        self.assertTrue("deck.gl" in msg or "Cesium" in msg, msg)
        self.assertEqual(p.added, [])            # no layer added to the TOC
        self.assertEqual(_FakeSceneLayer.instances, [])  # none constructed
        self.assertEqual(p.infos, [])            # no "Added ..." info
        self.assertEqual(p.zoomed, [])
        # the sniff did fetch the resolved, tokenized content GLB
        self.assertEqual(
            p._client.fetched,
            ["https://www.geoi.de/platform/tiles3d/services/5/0.glb"])

    def test_pnts_tileset_warns_without_any_fetch(self):
        p = self._plugin({"root": {"content": {"uri": "0.pnts"}}})
        p.add_tiles3d_layer({"id": 5, "title": "Scan"})
        self.assertEqual(len(p.bars), 1)
        self.assertEqual(p.added, [])
        self.assertEqual(p._client.fetched, [])  # .pnts short-circuits

    def test_mesh_tileset_proceeds_to_add(self):
        p = self._plugin({"root": {"content": {"uri": "0.glb"}}}, glb=_glb(4))
        p.add_tiles3d_layer({"id": 5, "title": "Scan"})
        self.assertEqual(p.bars, [])             # no point-cloud warning
        self.assertEqual(len(p.added), 1)        # layer added
        self.assertEqual(len(_FakeSceneLayer.instances), 1)
        self.assertEqual(len(p.infos), 1)
        self.assertIn("Added", p.infos[0])
        self.assertEqual(len(p.zoomed), 1)

    def test_sniff_error_fails_open_and_adds(self):
        # tiles3d_tileset_json raising must NOT block a legitimate mesh add.
        p = self._plugin({}, raise_json=True)
        p.add_tiles3d_layer({"id": 5, "title": "Scan"})
        self.assertEqual(p.bars, [])
        self.assertEqual(len(p.added), 1)
        self.assertEqual(len(p.infos), 1)

    def test_version_gate_short_circuits_before_the_sniff(self):
        # An old QGIS must warn about 3.34+ and never touch the client sniff.
        sys.modules["qgis.core"].Qgis = types.SimpleNamespace(
            QGIS_VERSION_INT=33200)
        p = self._plugin({"root": {"content": {"uri": "0.glb"}}}, glb=_glb(0))
        p.add_tiles3d_layer({"id": 5, "title": "Scan"})
        self.assertEqual(len(p.bars), 1)
        self.assertIn("3.34", p.bars[0])
        self.assertEqual(p._client.fetched, [])  # no wasted network call
        self.assertEqual(p.added, [])


# ------------------------------------------------------------ zoom-to-layers
class ZoomToLayersTest(unittest.TestCase):
    """The framing must union every layer's extent WITHOUT the deprecated
    ``QgsRectangle().setMinimal()`` sentinel."""

    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _install_qgis_geometry_stubs(self):
        qc = sys.modules["qgis.core"]

        class _Rect:
            def __init__(self, other=None):
                if other is None:
                    self.xmin = self.ymin = self.xmax = self.ymax = 0.0
                    self._empty = True
                else:
                    self.xmin, self.ymin = other.xmin, other.ymin
                    self.xmax, self.ymax = other.xmax, other.ymax
                    self._empty = other._empty

            @classmethod
            def of(cls, xmin, ymin, xmax, ymax):
                r = cls()
                r.xmin, r.ymin, r.xmax, r.ymax = xmin, ymin, xmax, ymax
                r._empty = False
                return r

            def isEmpty(self):
                return self._empty

            def combineExtentWith(self, other):
                if other.isEmpty():
                    return
                if self._empty:
                    self.xmin, self.ymin = other.xmin, other.ymin
                    self.xmax, self.ymax = other.xmax, other.ymax
                    self._empty = False
                    return
                self.xmin = min(self.xmin, other.xmin)
                self.ymin = min(self.ymin, other.ymin)
                self.xmax = max(self.xmax, other.xmax)
                self.ymax = max(self.ymax, other.ymax)

            def scale(self, _factor):
                pass

            def setMinimal(self):  # a canary: the fix must NEVER call this
                raise AssertionError("setMinimal() must not be called")

        class _Xform:
            def __init__(self, src, dst, project):
                pass

            def transformBoundingBox(self, ext):
                return ext  # identity — CRS handling is out of scope here

        qc.QgsRectangle = _Rect
        qc.QgsCoordinateTransform = _Xform
        qc.QgsProject = types.SimpleNamespace(instance=lambda: None)
        return _Rect

    def _plugin(self, extents, captured):
        rect = self._install_qgis_geometry_stubs()

        class _Layer:
            def __init__(self, ext):
                self._ext = ext

            def updateExtents(self):
                pass

            def extent(self):
                return self._ext

            def crs(self):
                return None

        class _Canvas:
            def mapSettings(self):
                return types.SimpleNamespace(
                    destinationCrs=lambda: None)

            def setExtent(self, box):
                captured.append(box)

            def refresh(self):
                captured.append("refresh")

        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p.iface = types.SimpleNamespace(mapCanvas=lambda: _Canvas())
        layers = [_Layer(rect.of(*e)) for e in extents]
        return p, layers

    def test_combined_extent_covers_the_union(self):
        captured = []
        p, layers = self._plugin(
            [(0.0, 0.0, 10.0, 10.0), (20.0, 5.0, 30.0, 25.0)], captured)
        p._zoom_to_layers(layers)
        box = captured[0]
        self.assertEqual(
            (box.xmin, box.ymin, box.xmax, box.ymax),
            (0.0, 0.0, 30.0, 25.0))
        self.assertIn("refresh", captured)

    def test_empty_extents_are_skipped(self):
        captured = []
        # a rect with _empty True is produced by the bare constructor
        rect = self._install_qgis_geometry_stubs()
        empty = rect()

        class _Layer:
            def updateExtents(self):
                pass

            def extent(self):
                return empty

            def crs(self):
                return None

        class _Canvas:
            def mapSettings(self):
                return types.SimpleNamespace(destinationCrs=lambda: None)

            def setExtent(self, box):
                captured.append(("setExtent", box))

            def refresh(self):
                captured.append("refresh")

        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p.iface = types.SimpleNamespace(mapCanvas=lambda: _Canvas())
        p._zoom_to_layers([_Layer()])
        # no non-empty extent -> setExtent never called, but canvas refreshed
        self.assertNotIn("setExtent", [c[0] if isinstance(c, tuple) else c
                                       for c in captured])
        self.assertIn("refresh", captured)


if __name__ == "__main__":
    unittest.main()
