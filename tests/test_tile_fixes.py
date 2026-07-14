"""Regressions from the raster-tile-service rollout (P0-P4).

Three independent bugs, one file:

* FIX 3 — a server-local (non-B2) presign returns a ROOT-RELATIVE
  ``/platform/raster/upload?t=…`` upload URL; ``put_url`` must absolutize it
  against ``base_url`` (raw urllib PUT to a scheme-less URL raises "unknown
  url type"), still carrying NO Authorization header.
* FIX 4 — ``CatalogTask`` must keep the CORE content (services/projects/
  folders) load unaffected when the OPTIONAL ``tile_services``/``list_groups``
  calls raise or time out, and must fetch those optional calls with a SHORT
  timeout so a hung endpoint can't stall sign-in.
* FIX 5 — the bearer authcfg must be rebuilt from the CURRENT live token, and
  a transient empty rebuild must NOT clobber a previously-good stored authcfg.

All pure / stdlib; qgis is stubbed where a module pulls it in at import.
"""

import io
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi.geoi_client import GeoiClient  # noqa: E402


class _FakeResp(io.BytesIO):
    def __init__(self, status=200):
        super().__init__(b"")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ----------------------------------------------------------------- FIX 3
class PutUrlAbsolutizeTest(unittest.TestCase):
    """A relative server-local upload URL is resolved against base_url; an
    absolute B2 URL is left unchanged; neither carries an Authorization
    header."""

    def _put(self, base_url, upload_url):
        captured = {}

        class _PutOpener:
            def open(self, req, timeout=None):
                captured["req"] = req
                return _FakeResp(200)

        c = GeoiClient(base_url=base_url)
        c.set_token("SECRET-BEARER")
        c._put_opener = _PutOpener()
        with tempfile.NamedTemporaryFile(suffix=".pmtiles", delete=False) as fh:
            fh.write(b"PMTILESBYTES")
            tmp = fh.name
        try:
            c.put_url(upload_url, tmp, "application/octet-stream")
        finally:
            os.unlink(tmp)
        return captured["req"]

    def test_relative_local_url_is_absolutized(self):
        req = self._put(
            "https://geoi.de", "/platform/raster/upload?t=abc"
        )
        self.assertEqual(
            req.full_url, "https://geoi.de/platform/raster/upload?t=abc",
            "a root-relative local upload URL must resolve against base_url",
        )
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertNotIn("authorization", headers,
                         "the local ?t= upload must not carry our bearer")

    def test_absolute_b2_url_is_unchanged(self):
        b2 = "https://s3.us-west.backblazeb2.com/bucket/u/1/x.pmtiles?X-Amz-Sig=z"
        req = self._put("https://geoi.de", b2)
        self.assertEqual(req.full_url, b2,
                         "an absolute B2 presigned URL must be used unchanged")
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertNotIn("authorization", headers,
                         "the B2 presigned PUT must not carry our bearer")

    def test_absolutize_helper_pure(self):
        c = GeoiClient(base_url="https://www.geoi.de")
        self.assertEqual(
            c._absolutize_upload_url("/platform/raster/upload?t=k"),
            "https://www.geoi.de/platform/raster/upload?t=k",
        )
        self.assertEqual(
            c._absolutize_upload_url("https://b2/x?sig=1"),
            "https://b2/x?sig=1",
        )
        self.assertEqual(c._absolutize_upload_url(""), "")
        # Protocol-relative is NOT same-origin — never prefixed with base_url.
        self.assertEqual(
            c._absolutize_upload_url("//evil.example/x"),
            "//evil.example/x",
        )


# ----------------------------------------------------------------- FIX 4
def _import_catalog_task():
    """Import ``tasks.CatalogTask`` with a minimal ``qgis.core`` stub so the
    module (which does ``from qgis.core import QgsTask``) imports off QGIS."""
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
    # Force a fresh import so the stub is the one bound.
    sys.modules.pop("geoi.tasks", None)
    from geoi import tasks  # noqa: E402

    def cleanup():
        sys.modules.pop("geoi.tasks", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return tasks, cleanup


class _CatalogClient:
    """Records the timeout each catalog call received, and can be told to make
    the optional calls fail."""

    def __init__(self, fail_optional=False):
        self.fail_optional = fail_optional
        self.timeouts = {}

    def list_services(self):
        return [{"name": "roads"}]

    def list_projects(self):
        return [{"id": 1}]

    def list_folders(self):
        return [{"id": "f1"}]

    def list_groups(self, timeout=None):
        self.timeouts["groups"] = timeout
        if self.fail_optional:
            raise RuntimeError("slow groups endpoint")
        return [{"id": "g1"}]

    def tile_services(self, timeout=None):
        self.timeouts["tiles"] = timeout
        if self.fail_optional:
            raise RuntimeError("tiles endpoint timed out")
        return [{"id": "t1"}]


class CatalogTaskTest(unittest.TestCase):
    def setUp(self):
        self.tasks, self._cleanup = _import_catalog_task()

    def tearDown(self):
        self._cleanup()

    def test_optional_failure_does_not_break_core_load(self):
        client = _CatalogClient(fail_optional=True)
        task = self.tasks.CatalogTask(client, on_done=None)
        result = task.work()
        # Core content is unaffected — the feature-services list still renders.
        self.assertEqual(result["services"], [{"name": "roads"}])
        self.assertEqual(result["projects"], [{"id": 1}])
        self.assertEqual(result["folders"], [{"id": "f1"}])
        # Optional buckets degrade to empty, not an exception.
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["tiles"], [])

    def test_optional_calls_use_short_timeout(self):
        client = _CatalogClient(fail_optional=False)
        task = self.tasks.CatalogTask(client, on_done=None)
        task.work()
        short = self.tasks.CatalogTask.OPTIONAL_TIMEOUT
        self.assertLess(short, 30, "optional calls must use a short timeout")
        self.assertEqual(client.timeouts.get("groups"), short)
        self.assertEqual(client.timeouts.get("tiles"), short)


class RequestTimeoutOverrideTest(unittest.TestCase):
    """``_request(timeout=...)`` overrides the client default for that one
    call (the mechanism FIX 4 relies on)."""

    def test_per_call_timeout_overrides_default(self):
        seen = {}

        class _Opener:
            def open(self, req, timeout=None):
                seen["timeout"] = timeout
                return _FakeResp(200)

        c = GeoiClient(base_url="https://geoi.de", timeout=30, opener=_Opener())
        c.tile_services(timeout=5)
        self.assertEqual(seen["timeout"], 5)
        c.list_services()
        self.assertEqual(seen["timeout"], 30, "default timeout otherwise")


# ----------------------------------------------------------------- FIX 5
def _bare_plugin():
    """A ``GeoiPlugin`` instance built WITHOUT ``__init__`` (which needs the
    Qt widget tree), with qgis stubbed so the module imports. Only the
    attributes ``_sync_header_authcfg`` touches are wired."""
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

    # browser_panel / dialogs / auth / tasks pull in qgis too; stub them as
    # opaque modules so plugin's `from .gui... import ...` succeeds.
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


class _FakeStore:
    """Records the (token, existing_id) ensure_header_authcfg was called with
    and returns a scripted authcfg id."""

    def __init__(self, returns="cfg-NEW"):
        self.returns = returns
        self.calls = []

    def ensure_header_authcfg(self, token, existing_id=""):
        self.calls.append((token, existing_id))
        return self.returns


class _FakeSettings:
    """A stand-in for the ``settings`` module's authcfg get/set pair."""

    def __init__(self, stored=""):
        self._stored = stored

    def header_authcfg(self):
        return self._stored

    def set_header_authcfg(self, value):
        self._stored = value or ""


class SyncHeaderAuthcfgTest(unittest.TestCase):
    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()
        self.GeoiPlugin = self.plugin_mod.GeoiPlugin

    def tearDown(self):
        self._cleanup()

    def _plugin(self, token, store, fake_settings):
        p = self.GeoiPlugin.__new__(self.GeoiPlugin)
        p._client = types.SimpleNamespace(token=token)
        p._store = store
        # Point the module-level `settings` reference at our fake.
        self.plugin_mod.settings = fake_settings
        return p

    def test_authcfg_rebuilt_from_current_token(self):
        store = _FakeStore(returns="cfg-NEW")
        cfg = _FakeSettings(stored="cfg-OLD")
        p = self._plugin("LIVE-TOKEN", store, cfg)
        out = p._sync_header_authcfg()
        # Rebuilt from the CURRENT token, persisted, and returned.
        self.assertEqual(store.calls, [("LIVE-TOKEN", "cfg-OLD")])
        self.assertEqual(out, "cfg-NEW")
        self.assertEqual(cfg.header_authcfg(), "cfg-NEW")

    def test_transient_failure_keeps_stored_authcfg(self):
        # ensure_header_authcfg returns "" (e.g. auth DB momentarily locked).
        store = _FakeStore(returns="")
        cfg = _FakeSettings(stored="cfg-GOOD")
        p = self._plugin("LIVE-TOKEN", store, cfg)
        out = p._sync_header_authcfg()
        # The good stored id is NOT clobbered with "".
        self.assertEqual(cfg.header_authcfg(), "cfg-GOOD")
        self.assertEqual(out, "cfg-GOOD")

    def test_signed_out_returns_empty_without_touching_store(self):
        store = _FakeStore(returns="cfg-NEW")
        cfg = _FakeSettings(stored="cfg-OLD")
        p = self._plugin(None, store, cfg)
        out = p._sync_header_authcfg()
        self.assertEqual(out, "")
        self.assertEqual(store.calls, [], "no rebuild attempt when signed out")
        self.assertEqual(cfg.header_authcfg(), "cfg-OLD", "stored id untouched")


# ----------------------------------------------------------------- WS4
class ParseBoundsTest(unittest.TestCase):
    """The pure WGS84 bounds normaliser accepts a list AND the two dict
    conventions, and rejects anything unparseable (-> None -> skip)."""

    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def test_list_form(self):
        self.assertEqual(
            self.plugin_mod._parse_bounds([1.0, 2.0, 3.0, 4.0]),
            (1.0, 2.0, 3.0, 4.0))

    def test_minmax_dict(self):
        self.assertEqual(
            self.plugin_mod._parse_bounds(
                {"minx": 1, "miny": 2, "maxx": 3, "maxy": 4}),
            (1.0, 2.0, 3.0, 4.0))

    def test_compass_dict(self):
        self.assertEqual(
            self.plugin_mod._parse_bounds(
                {"west": -1, "south": -2, "east": 5, "north": 6}),
            (-1.0, -2.0, 5.0, 6.0))

    def test_unparseable_is_none(self):
        for bad in (None, [], [1, 2, 3], {"minx": 1}, "nope", 42,
                    {"minx": "x", "miny": 2, "maxx": 3, "maxy": 4}):
            self.assertIsNone(self.plugin_mod._parse_bounds(bad))


class SetLayerExtentFromBoundsTest(unittest.TestCase):
    """WS4 — with bounds present the layer extent is set to the reprojected
    service bounds; None/absent bounds -> setExtent is never called."""

    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _install_stubs(self):
        qc = sys.modules["qgis.core"]

        class _Rect:
            def __init__(self, w, s, e, n):
                self.coords = (w, s, e, n)

        class _CRS:
            def __init__(self, authid):
                self.authid = authid

        class _Xform:
            def __init__(self, src, dst, project):
                self.src, self.dst = src, dst

            def transformBoundingBox(self, rect):
                # "reproject" = double every ordinate, so the test can prove the
                # transform actually ran (identity would be indistinguishable).
                w, s, e, n = rect.coords
                return _Rect(w * 2, s * 2, e * 2, n * 2)

        qc.QgsRectangle = _Rect
        qc.QgsCoordinateReferenceSystem = _CRS
        qc.QgsCoordinateTransform = _Xform
        qc.QgsProject = types.SimpleNamespace(instance=lambda: None)
        return _Rect

    def _layer(self, captured):
        class _Layer:
            def crs(self):
                return "EPSG:3857"

            def setExtent(self, rect):
                captured.append(rect)

        return _Layer()

    def test_bounds_present_sets_reprojected_extent(self):
        self._install_stubs()
        captured = []
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._set_layer_extent_from_bounds(
            self._layer(captured), [10.0, 20.0, 30.0, 40.0])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].coords, (20.0, 40.0, 60.0, 80.0))

    def test_dict_bounds_also_work(self):
        self._install_stubs()
        captured = []
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._set_layer_extent_from_bounds(
            self._layer(captured),
            {"minx": 1.0, "miny": 2.0, "maxx": 3.0, "maxy": 4.0})
        self.assertEqual(captured[0].coords, (2.0, 4.0, 6.0, 8.0))

    def test_none_bounds_never_calls_set_extent(self):
        self._install_stubs()
        captured = []
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)
        p._set_layer_extent_from_bounds(self._layer(captured), None)
        self.assertEqual(captured, [])


class TileDetailReturnsBoundsTest(unittest.TestCase):
    """``_tile_detail`` returns (urls, bounds, err) from ONE tile_service
    fetch, and the legacy ``_tile_format_urls`` still returns (urls, err)."""

    def setUp(self):
        self.plugin_mod, self._cleanup = _bare_plugin()

    def tearDown(self):
        self._cleanup()

    def _plugin(self, detail):
        p = self.plugin_mod.GeoiPlugin.__new__(self.plugin_mod.GeoiPlugin)

        class _Client:
            base_url = "https://geoi.de"

            def tile_service(self, sid):
                return detail

            def tile_format_urls(self, d):
                return {"xyz": "https://x/{z}/{x}/{y}.webp"}

        p._client = _Client()
        return p

    def test_bounds_read_from_detail_in_one_fetch(self):
        p = self._plugin({"id": 5, "bounds": [1, 2, 3, 4],
                          "urls": {"xyz": "/x"}})
        urls, bounds, err = p._tile_detail({"id": 5})
        self.assertIsNone(err)
        self.assertEqual(bounds, [1, 2, 3, 4])
        self.assertIn("xyz", urls)
        # Back-compat shim keeps the 2-tuple shape for copy_tile_url.
        urls2, err2 = p._tile_format_urls({"id": 5})
        self.assertIsNone(err2)
        self.assertEqual(urls2, urls)

    def test_absent_bounds_is_none(self):
        p = self._plugin({"id": 5, "urls": {"xyz": "/x"}})
        _urls, bounds, err = p._tile_detail({"id": 5})
        self.assertIsNone(err)
        self.assertIsNone(bounds)


if __name__ == "__main__":
    unittest.main()
