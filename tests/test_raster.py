"""Pure tests for the raster -> PMTiles -> presigned-upload pipeline (#552).

No QGIS and no real GDAL: ``geoi.raster`` imports ``osgeo`` lazily inside its
functions, so a fake ``osgeo`` package injected into ``sys.modules`` lets us
drive the pipeline and capture exactly what it asks GDAL to do — the contract
that matters here is "EPSG:3857 is forced, with no CRS parameter anywhere".
"""

import inspect
import io
import os
import sys
import types
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import raster  # noqa: E402


class _FakeDataset:
    """A minimal GDAL dataset stub returning a chosen projection WKT."""

    def __init__(self, wkt=""):
        self._wkt = wkt

    def GetProjection(self):
        return self._wkt

    def FlushCache(self):
        pass


def _install_fake_osgeo(gdal_obj, srs_authority=None):
    """Install a fake ``osgeo`` package (gdal + osr) into sys.modules.

    Returns a cleanup callable that restores the previous modules.
    """
    saved = {k: sys.modules.get(k) for k in ("osgeo", "osgeo.gdal", "osgeo.osr")}

    # raster._gdal() calls gdal.UseExceptions(); give every fake one a no-op.
    if not hasattr(gdal_obj, "UseExceptions"):
        gdal_obj.UseExceptions = lambda *a, **k: None

    osgeo = types.ModuleType("osgeo")
    osgeo.gdal = gdal_obj

    class _SRS:
        def __init__(self):
            self._auth = srs_authority

        def ImportFromWkt(self, wkt):
            # The fake encodes the authority in the dataset; ignore the WKT.
            pass

        def GetAuthorityName(self, _):
            return self._auth[0] if self._auth else None

        def GetAuthorityCode(self, _):
            return self._auth[1] if self._auth else None

    osr = types.ModuleType("osgeo.osr")
    osr.SpatialReference = _SRS

    osgeo.osr = osr
    sys.modules["osgeo"] = osgeo
    sys.modules["osgeo.gdal"] = gdal_obj
    sys.modules["osgeo.osr"] = osr

    def cleanup():
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return cleanup


class TargetCrsConstantTest(unittest.TestCase):
    def test_target_crs_is_web_mercator(self):
        self.assertEqual(raster.TARGET_CRS, "EPSG:3857")


class WarpForcesWebMercatorTest(unittest.TestCase):
    def test_warp_signature_has_no_crs_parameter(self):
        # The whole point of #552: there is NO caller-supplied CRS — 3857 is
        # forced. Inspect the public signature and assert no CRS knob exists.
        params = set(inspect.signature(raster.warp_to_3857).parameters)
        for forbidden in ("crs", "dst_srs", "dstSRS", "target_crs", "epsg", "srs"):
            self.assertNotIn(forbidden, params,
                             "warp_to_3857 must not expose a CRS parameter")

    def test_warp_calls_gdal_with_dst_srs_3857(self):
        captured = {}

        class _Gdal:
            def Warp(self, dst, src, **kwargs):
                captured["dst"] = dst
                captured["kwargs"] = kwargs
                return _FakeDataset()  # truthy; FlushCache() then None

            def Open(self, path):
                # assert_target_crs re-opens — hand it a 3857 dataset.
                return _FakeDataset(wkt="PROJCS[...]")

        cleanup = _install_fake_osgeo(_Gdal(), srs_authority=("EPSG", "3857"))
        try:
            out = raster.warp_to_3857("/tmp/in.vrt", "/tmp/out.3857.tif")
        finally:
            cleanup()

        self.assertEqual(out, "/tmp/out.3857.tif")
        self.assertEqual(captured["kwargs"].get("dstSRS"), "EPSG:3857")
        # the value is exactly the module constant, never anything else
        self.assertEqual(captured["kwargs"]["dstSRS"], raster.TARGET_CRS)


class TransparentBackgroundTest(unittest.TestCase):
    """#680: the reprojected/rotated source must NOT leave a black wedge
    around the data. The warp adds an alpha band so uncovered pixels are
    TRANSPARENT, and the WebP tiles are encoded LOSSLESS so that alpha
    survives into the PMTiles — black background gone, end to end."""

    def test_warp_requests_an_alpha_band(self):
        captured = {}

        class _Gdal:
            def Warp(self, dst, src, **kwargs):
                captured["kwargs"] = kwargs
                return _FakeDataset()

            def Open(self, path):
                return _FakeDataset(wkt="PROJCS[...]")

        cleanup = _install_fake_osgeo(_Gdal(), srs_authority=("EPSG", "3857"))
        try:
            raster.warp_to_3857("/tmp/in.vrt", "/tmp/out.3857.tif")
        finally:
            cleanup()

        # dstAlpha=True is what makes the no-data background transparent
        # instead of opaque black — the core of the #680 fix.
        self.assertTrue(
            captured["kwargs"].get("dstAlpha"),
            "warp must add an alpha band so uncovered areas are transparent",
        )

    def test_webp_tiles_are_encoded_lossless_to_keep_alpha(self):
        captured = {}

        class _Translated:
            def BuildOverviews(self, *a, **k):
                pass

            def FlushCache(self):
                pass

        class _Gdal:
            def Open(self, path):
                return _FakeDataset(wkt="PROJCS[...]")

            def Translate(self, dst, src, **kwargs):
                captured["kwargs"] = kwargs
                # The MBTiles must exist so to_pmtiles continues to the
                # converter step; touch an empty file for the .pmtiles check.
                return _Translated()

        # Stub the MBTiles->PMTiles conversion + the output existence check so
        # the test stays a pure unit on the encoding options. Force the
        # python writer path so a real `pmtiles` CLI on PATH can't reroute it.
        orig_conv = raster._mbtiles_to_pmtiles_python
        orig_writer = raster._pmtiles_writer
        orig_exists = os.path.exists
        raster._mbtiles_to_pmtiles_python = lambda mb, pm: None
        raster._pmtiles_writer = lambda: ("python", "vendored")

        def _exists(path):
            if str(path).endswith(".pmtiles"):
                return True
            return orig_exists(path)

        cleanup = _install_fake_osgeo(_Gdal(), srs_authority=("EPSG", "3857"))
        try:
            os.path.exists = _exists
            raster.to_pmtiles("/tmp/out.3857.tif", "/tmp/out.pmtiles")
        finally:
            os.path.exists = orig_exists
            raster._mbtiles_to_pmtiles_python = orig_conv
            raster._pmtiles_writer = orig_writer
            cleanup()

        opts = captured["kwargs"].get("creationOptions") or []
        self.assertIn(
            "TILE_FORMAT=WEBP", opts, "tiles must stay WebP (alpha-capable)"
        )
        # Lossless WebP (QUALITY=100) is what preserves the alpha band; lossy
        # WebP in the MBTiles driver drops it and the black border returns.
        self.assertIn(
            "QUALITY=100", opts,
            "WebP must be lossless so the transparent alpha band survives",
        )


class AssertTargetCrsTest(unittest.TestCase):
    def test_passes_on_a_3857_dataset(self):
        cleanup = _install_fake_osgeo(type("g", (), {})(),
                                      srs_authority=("EPSG", "3857"))
        try:
            ds = _FakeDataset(wkt="PROJCS[...]")
            self.assertTrue(raster.assert_target_crs(ds))
        finally:
            cleanup()

    def test_raises_on_a_non_3857_dataset(self):
        cleanup = _install_fake_osgeo(type("g", (), {})(),
                                      srs_authority=("EPSG", "4326"))
        try:
            ds = _FakeDataset(wkt="GEOGCS[...]")
            with self.assertRaises(raster.RasterError):
                raster.assert_target_crs(ds)
        finally:
            cleanup()


def _drop_pmtiles_from_sys_modules():
    """Remove every cached ``pmtiles`` / ``pmtiles.*`` module.

    A forced vendored import (``force_vendored=True``) caches the bundled
    package under the bare name ``pmtiles`` — which would then make the
    NEXT ``_pmtiles_writer()`` see a "system" pmtiles. Tests that probe
    writer resolution clean these out so cases don't leak into each other.
    """
    for name in [m for m in list(sys.modules)
                 if m == "pmtiles" or m.startswith("pmtiles.")]:
        del sys.modules[name]


class _PmtilesWriterEnv:
    """Context manager that stubs ``shutil.which`` + the importability of a
    system ``pmtiles`` package, then restores both.

    ``which`` — a function ``name -> path|None`` (defaults to "nothing on
    PATH"). ``system`` — when ``False`` poison ``sys.modules['pmtiles']``
    so an ``import pmtiles.convert`` raises ImportError; when a module
    object, install it as the importable system copy.
    """

    def __init__(self, which=None, system=False):
        self._which = which or (lambda name: None)
        self._system = system

    def __enter__(self):
        import shutil
        self._shutil = shutil
        self._orig_which = shutil.which
        self._saved = {k: sys.modules.get(k)
                       for k in ("pmtiles", "pmtiles.convert")}
        _drop_pmtiles_from_sys_modules()
        shutil.which = self._which
        if self._system is False:
            # Poison so `import pmtiles[.convert]` -> ImportError.
            sys.modules["pmtiles"] = None
            sys.modules["pmtiles.convert"] = None
        elif self._system is not None:
            pkg = types.ModuleType("pmtiles")
            pkg.__path__ = []  # mark as a package
            sys.modules["pmtiles"] = pkg
            sys.modules["pmtiles.convert"] = self._system
        return self

    def __exit__(self, *exc):
        self._shutil.which = self._orig_which
        _drop_pmtiles_from_sys_modules()
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        return False


class PmtilesWriterResolutionTest(unittest.TestCase):
    """``_pmtiles_writer()`` ALWAYS resolves a writer — the bundled
    pure-Python copy is the guaranteed floor (#552, bundled-pmtiles)."""

    def test_vendored_writer_is_the_guaranteed_floor(self):
        # No CLI on PATH and no importable system `pmtiles` -> the bundled
        # copy. A writer is ALWAYS available; the old (None, None) /
        # "install pmtiles" failure is gone.
        with _PmtilesWriterEnv(which=lambda name: None, system=False):
            self.assertEqual(raster._pmtiles_writer(), ("python", "vendored"))

    def test_prefers_system_pmtiles_over_vendored(self):
        # A user's installed `pmtiles` must win, and we must hand back THAT
        # module's mbtiles_to_pmtiles — never clobber the user's install
        # with our vendored copy.
        fake_convert = types.ModuleType("pmtiles.convert")
        sentinel = object()
        fake_convert.mbtiles_to_pmtiles = sentinel
        with _PmtilesWriterEnv(which=lambda name: None, system=fake_convert):
            self.assertEqual(raster._pmtiles_writer(), ("python", "system"))
            self.assertIs(raster._vendored_pmtiles_convert(), sentinel)

    def test_prefers_cli_when_on_path(self):
        # A native `pmtiles` CLI on PATH is the fastest path and wins.
        with _PmtilesWriterEnv(which=lambda name: "/usr/local/bin/pmtiles",
                               system=False):
            self.assertEqual(raster._pmtiles_writer(),
                             ("cli", "/usr/local/bin/pmtiles"))


# A minimal valid 1x1 WebP RIFF blob (lossy VP8). Enough for the PMTiles
# writer, which just copies the tile bytes through verbatim.
_WEBP_1x1 = (
    b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00"
    b"\x10\x00\x00\x00\x00\x9d\x01*\x01\x00\x01\x00"
)


def _build_webp_mbtiles(path):
    """Write a tiny valid WebP MBTiles (one z0 1x1 tile) with stdlib sqlite3.

    No GDAL/QGIS needed — this is the exact shape the PMTiles converter
    consumes, so it exercises the bundled writer end to end.
    """
    import sqlite3

    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE metadata (name text, value text);")
        con.execute(
            "CREATE TABLE tiles (zoom_level integer, tile_column integer, "
            "tile_row integer, tile_data blob);"
        )
        for name, value in (
            ("format", "webp"),
            ("minzoom", "0"),
            ("maxzoom", "0"),
            ("bounds", "-180.0,-85.05112878,180.0,85.05112878"),
        ):
            con.execute("INSERT INTO metadata VALUES (?, ?);", (name, value))
        con.execute(
            "INSERT INTO tiles VALUES (0, 0, 0, ?);",
            (sqlite3.Binary(_WEBP_1x1),),
        )
        con.commit()
    finally:
        con.close()


class RealVendoredConversionTest(unittest.TestCase):
    """The BUNDLED pure-Python writer genuinely produces a valid PMTiles v3
    with NO external tool (no CLI, no pip install, no GDAL/QGIS)."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)
        # The forced vendored import caches `pmtiles`; clear it so the
        # writer-resolution tests aren't polluted by ordering.
        _drop_pmtiles_from_sys_modules()

    def test_vendored_convert_writes_pmtiles_v3(self):
        mbtiles = os.path.join(self._tmp, "in.mbtiles")
        out = os.path.join(self._tmp, "out.pmtiles")
        _build_webp_mbtiles(mbtiles)

        # Force the bundled module and call it with the 3-arg signature
        # (input, output, maxzoom=None) the writer wrapper uses.
        convert = raster._vendored_pmtiles_convert(force_vendored=True)
        convert(mbtiles, out, None)

        self.assertTrue(os.path.exists(out), "no PMTiles file was written")
        with open(out, "rb") as fh:
            head = fh.read(8)
        # PMTiles v3 header: magic b"PMTiles" then a version byte == 3.
        self.assertEqual(head[:7], b"PMTiles",
                         "output is not a PMTiles archive")
        self.assertEqual(head[7], 3, "PMTiles spec version must be 3")


class _FakeResp(io.BytesIO):
    def __init__(self, status=200):
        super().__init__(b"")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PutUrlNoAuthTest(unittest.TestCase):
    """The presigned object-store PUT must carry NO Authorization header —
    sending the geoi bearer to Backblaze would leak our token off-site."""

    def test_put_url_sends_no_authorization_header(self):
        from geoi.geoi_client import GeoiClient

        captured = {}

        class _PutOpener:
            def open(self, req, timeout=None):
                captured["req"] = req
                return _FakeResp(200)

        c = GeoiClient(base_url="https://geoi.de")
        c.set_token("SECRET-BEARER")
        c._put_opener = _PutOpener()

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pmtiles", delete=False) as fh:
            fh.write(b"PMTILESBYTES")
            tmp = fh.name
        try:
            c.put_url("https://s3.example.com/bucket/u/1/x.pmtiles", tmp,
                      "application/octet-stream")
        finally:
            os.unlink(tmp)

        req = captured["req"]
        self.assertIsInstance(req, urllib.request.Request)
        self.assertEqual(req.get_method(), "PUT")
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertNotIn("authorization", headers,
                         "the presigned PUT must never carry our bearer")


class RasterListKeyTest(unittest.TestCase):
    """raster_list() must read the SAME key the server emits ("layers",
    api/raster.php) — a mismatch silently returns an empty list."""

    def test_raster_list_reads_layers_key(self):
        from geoi.geoi_client import GeoiClient

        c = GeoiClient(base_url="https://geoi.de")
        rows = [{"id": 1, "name": "ortho", "public_url": "https://cdn/x.pmtiles"}]
        c._request = lambda method, path, **kw: {"ok": True, "layers": rows}
        self.assertEqual(c.raster_list(), rows)

        # The legacy "rasters" key must NOT be what it reads.
        c._request = lambda method, path, **kw: {"ok": True, "rasters": rows}
        self.assertEqual(c.raster_list(), [])


if __name__ == "__main__":
    unittest.main()
