"""Pure tests for the 3D Tiles publish path (PR-7 — QGIS plugin parity).

No QGIS: ``geoi.tiles3d`` is stdlib-only (zipfile) and the client speaks over
an injected fake opener, so this drives the whole "upload a prepared tileset
ZIP → 3D Tiles service" flow the way test_raster.py drives the raster pipeline.

The contract that matters here:
  * a valid tileset ZIP has a ROOT-level tileset.json (a nested-only one fails,
    fast, BEFORE any upload);
  * the create call is a multipart POST to /platform/tiles3d/create with the
    file part named ``archive`` and the geoi bearer attached;
  * a server ``quota_exceeded`` maps to the same friendly, actionable sentence
    pattern as the raster path.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import geoi_client, tiles3d  # noqa: E402
from geoi.geoi_client import GeoiClient, GeoiError  # noqa: E402


# --------------------------------------------------------------------- fakes
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


def _client(responses):
    return GeoiClient(base_url="https://geoi.de", opener=_FakeOpener(responses))


# ------------------------------------------------------------------ ZIP builders
def _write_zip(path, entries):
    """Write a ZIP with ``entries`` = {arcname: bytes}."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


_TILESET_JSON = json.dumps(
    {"asset": {"version": "1.1"}, "geometricError": 100,
     "root": {"geometricError": 0, "content": {"uri": "0.glb"}}}
).encode("utf-8")


def _good_tileset_zip(tmp):
    """A valid tileset: ROOT tileset.json + one .glb tile."""
    return _write_zip(os.path.join(tmp, "scan.zip"),
                      {"tileset.json": _TILESET_JSON, "0.glb": b"GLBDATA"})


def _nested_only_zip(tmp):
    """tileset.json only inside a sub-folder — NOT a valid root tileset."""
    return _write_zip(os.path.join(tmp, "nested.zip"),
                      {"sub/tileset.json": _TILESET_JSON, "sub/0.glb": b"GLB"})


# ----------------------------------------------------------------------- tests
class ValidateTilesetZipTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_good_zip_passes_and_counts_tiles(self):
        summary = tiles3d.validate_tileset_zip(_good_tileset_zip(self._tmp))
        self.assertEqual(summary["tileCount"], 1)
        self.assertEqual(summary["entries"], 2)

    def test_nested_only_tileset_json_is_rejected(self):
        # The platform serves the tileset from <prefix>/tileset.json at the
        # ROOT — a sub-folder-only tileset.json would publish a dead service.
        with self.assertRaises(tiles3d.Tiles3dError) as ctx:
            tiles3d.validate_tileset_zip(_nested_only_zip(self._tmp))
        self.assertIn("root", str(ctx.exception).lower())

    def test_zip_without_any_tileset_json_is_rejected(self):
        path = _write_zip(os.path.join(self._tmp, "no-ts.zip"),
                          {"0.glb": b"GLB", "meta.txt": b"hi"})
        with self.assertRaises(tiles3d.Tiles3dError) as ctx:
            tiles3d.validate_tileset_zip(path)
        self.assertIn("tileset.json", str(ctx.exception))

    def test_non_zip_file_is_rejected(self):
        path = os.path.join(self._tmp, "not.zip")
        with open(path, "wb") as fh:
            fh.write(b"this is not a zip")
        with self.assertRaises(tiles3d.Tiles3dError):
            tiles3d.validate_tileset_zip(path)

    def test_missing_file_is_rejected(self):
        with self.assertRaises(tiles3d.Tiles3dError):
            tiles3d.validate_tileset_zip(os.path.join(self._tmp, "nope.zip"))

    def test_default_title_is_the_file_stem(self):
        self.assertEqual(tiles3d.default_title("/x/y/My Scan.zip"), "My Scan")
        self.assertEqual(tiles3d.default_title(""), "3D tiles")


class Tiles3dCreateClientTest(unittest.TestCase):
    """The create call is a multipart POST to /tiles3d/create — file part
    ``archive`` (the exact field api/tiles3d.php reads) + the geoi bearer."""

    def test_create_posts_multipart_archive_with_bearer(self):
        c = _client([(json.dumps({"ok": True, "service": {
            "id": 7, "title": "Scan",
            "urls": {"tileset": "https://geoi.de/platform/tiles3d/services/7/tileset.json"},
        }}), 200)])
        c.set_token("SESS-TOKEN")
        svc = c.tiles3d_create(b"PK\x03\x04ZIPBYTES", title="Scan",
                               filename="scan.zip")
        self.assertEqual(svc["id"], 7)

        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/platform/tiles3d/create"))
        headers = {k.lower(): v for k, v in req.header_items()}
        # Multipart, the bearer rides (owner-only endpoint).
        self.assertTrue(headers["content-type"].startswith(
            "multipart/form-data; boundary="))
        self.assertEqual(headers["authorization"], "Bearer SESS-TOKEN")
        # The file part MUST be named "archive"; the title rides as a plain
        # form field (no filename) so it lands in PHP's $_POST.
        self.assertIn(b'name="archive"; filename="scan.zip"', req.data)
        self.assertIn(b'name="title"', req.data)
        self.assertIn(b"ZIPBYTES", req.data)

    def test_create_reads_a_zip_from_a_path(self):
        tmp = tempfile.mkdtemp()
        try:
            path = _good_tileset_zip(tmp)
            c = _client([(json.dumps({"ok": True, "service": {
                "id": 3, "title": "scan", "urls": {"tileset": "/x/3/tileset.json"}}}), 200)])
            c.set_token("T")
            svc = c.tiles3d_create(path, title="scan")
            self.assertEqual(svc["id"], 3)
            self.assertIn(b'name="archive"; filename="scan.zip"',
                          c._opener.requests[0].data)
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_create_2xx_without_service_raises(self):
        c = _client([(json.dumps({"ok": True}), 200)])
        c.set_token("T")
        with self.assertRaises(GeoiError):
            c.tiles3d_create(b"ZIP", title="x")

    def test_create_quota_exceeded_maps_to_friendly_text(self):
        # The platform answers a full quota with a flat `error` code + 413.
        c = _client([(json.dumps({"ok": False, "error": "quota_exceeded",
                                  "message": "quota full"}), 413)])
        c.set_token("T")
        with self.assertRaises(GeoiError) as ctx:
            c.tiles3d_create(b"ZIP", title="x")
        self.assertEqual(ctx.exception.code, "quota_exceeded")
        self.assertIn("quota", geoi_client.tiles3d_friendly_error(ctx.exception).lower())


class Tiles3dManageClientTest(unittest.TestCase):
    def test_list_unwraps_services(self):
        c = _client([(json.dumps({"ok": True, "services": [
            {"id": 1, "title": "A"}, {"id": 2, "title": "B"}]}), 200)])
        c.set_token("T")
        rows = c.tiles3d_list()
        self.assertEqual([r["id"] for r in rows], [1, 2])
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.endswith("/platform/tiles3d/services"))

    def test_list_missing_or_malformed_is_empty(self):
        for body in ({"ok": True}, {"services": "nope"}):
            c = _client([(json.dumps(body), 200)])
            self.assertEqual(c.tiles3d_list(), [])

    def test_get_unwraps_detail(self):
        c = _client([(json.dumps({"ok": True, "service": {
            "id": 5, "urls": {"tileset": "/x/5/tileset.json"}}}), 200)])
        c.set_token("T")
        detail = c.tiles3d_get(5)
        self.assertEqual(detail["id"], 5)
        self.assertTrue(
            c._opener.requests[0].full_url.endswith("/platform/tiles3d/services/5"))

    def test_delete_issues_delete_and_returns_true(self):
        c = _client([(json.dumps({"ok": True}), 200)])
        c.set_token("T")
        self.assertTrue(c.tiles3d_delete(9))
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "DELETE")
        self.assertTrue(req.full_url.endswith("/platform/tiles3d/services/9"))

    def test_tileset_url_absolutizes_relative_and_keeps_absolute(self):
        c = _client([])
        self.assertEqual(
            c.tiles3d_tileset_url("/platform/tiles3d/services/5/tileset.json"),
            "https://geoi.de/platform/tiles3d/services/5/tileset.json")
        absolute = "https://cdn.example.com/t/5/tileset.json"
        self.assertEqual(c.tiles3d_tileset_url(absolute), absolute)
        self.assertEqual(c.tiles3d_tileset_url(""), "")


class Tiles3dPublishFlowTest(unittest.TestCase):
    """End-to-end: tiles3d.publish() validates, uploads and shapes the result;
    a server error surfaces as a friendly Tiles3dError."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_publish_happy_path_returns_absolute_tileset_url(self):
        c = _client([(json.dumps({"ok": True, "service": {
            "id": 11, "title": "scan",
            "urls": {"tileset": "/platform/tiles3d/services/11/tileset.json"}}}), 200)])
        c.set_token("T")
        out = tiles3d.publish(c, _good_tileset_zip(self._tmp), title="")
        self.assertEqual(out["id"], 11)
        self.assertEqual(out["title"], "scan")
        self.assertEqual(
            out["tilesetUrl"],
            "https://geoi.de/platform/tiles3d/services/11/tileset.json")

    def test_publish_defaults_title_to_the_file_stem(self):
        c = _client([(json.dumps({"ok": True, "service": {
            "id": 1, "urls": {"tileset": "/x/1/tileset.json"}}}), 200)])
        c.set_token("T")
        tiles3d.publish(c, _good_tileset_zip(self._tmp), title="   ")
        # The empty title fell back to the ZIP stem "scan" as a plain field.
        self.assertIn(b"scan", c._opener.requests[0].data)

    def test_publish_rejects_a_bad_zip_before_any_upload(self):
        c = _client([])  # no queued response — an upload would raise IndexError
        c.set_token("T")
        with self.assertRaises(tiles3d.Tiles3dError):
            tiles3d.publish(c, _nested_only_zip(self._tmp))
        self.assertEqual(c._opener.requests, [])  # nothing was sent

    def test_publish_maps_server_error_to_friendly_tiles3d_error(self):
        c = _client([(json.dumps({"ok": False, "error": "feature_off",
                                  "message": "3D tiles off"}), 503)])
        c.set_token("T")
        with self.assertRaises(tiles3d.Tiles3dError) as ctx:
            tiles3d.publish(c, _good_tileset_zip(self._tmp))
        self.assertIn("administrator", str(ctx.exception))


class LooksLikePointCloudTest(unittest.TestCase):
    def test_point_cloud_extensions(self):
        for path in ("/x/scan.las", "C:\\d\\scan.LAZ", "cloud.ply",
                     "/a/b/UPPER.PLY"):
            self.assertTrue(tiles3d.looks_like_point_cloud(path), path)

    def test_everything_else_is_not(self):
        for path in ("scan.zip", "scan.las.txt", "photo.jpg", "", None,
                     "las", "plyfile"):
            self.assertFalse(tiles3d.looks_like_point_cloud(path), path)


# --------------------------------------------- point-cloud publish flow
class _RecordingTiles3dClient:
    """A fake client for the point-cloud lane: records the create call.
    ``accept_bounds`` toggles whether ``tiles3d_create`` takes the ``bounds``
    kwarg (a newer vs the current client signature)."""

    def __init__(self, accept_bounds=False, error=None):
        self.calls = []
        self.error = error
        if accept_bounds:
            self.tiles3d_create = self._create_with_bounds
        else:
            self.tiles3d_create = self._create_plain

    def _respond(self, zip_bytes, title, filename, bounds=None):
        if self.error is not None:
            raise self.error
        self.calls.append({"zip": zip_bytes, "title": title,
                           "filename": filename, "bounds": bounds})
        return {"id": 42, "title": title,
                "urls": {"tileset": "/platform/tiles3d/services/42/tileset.json"}}

    def _create_plain(self, zip_path_or_bytes, title="", filename=None):
        return self._respond(zip_path_or_bytes, title, filename)

    def _create_with_bounds(self, zip_path_or_bytes, title="", filename=None,
                            bounds=None):
        return self._respond(zip_path_or_bytes, title, filename, bounds)

    def tiles3d_tileset_url(self, raw_url):
        url = (raw_url or "").strip()
        if url and "://" not in url:
            url = "https://geoi.de/" + url.lstrip("/")
        return url


def _write_ply(tmp, name="cloud.ply"):
    """A tiny georeference-less PLY on disk (the keep-local lane)."""
    import test_pointcloud as tp

    pts = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (2.0, 3.0, 4.0)]
    path = os.path.join(tmp, name)
    with open(path, "wb") as fh:
        fh.write(tp.make_ply(pts, [(10, 20, 30)] * 3))
    return path


def _write_wgs84_las(tmp, name="survey.las"):
    """A small georeferenced WGS84 LAS on disk (the reproject lane)."""
    import test_pointcloud as tp

    pts, cols = tp._wgs84_points(30)
    path = os.path.join(tmp, name)
    with open(path, "wb") as fh:
        fh.write(tp.make_las(pts, colors=cols, wkt=tp.WGS84_WKT))
    return path


class PublishPointCloudTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_posts_a_zip_with_a_root_tileset_json(self):
        c = _RecordingTiles3dClient()
        out = tiles3d.publish_point_cloud(c, _write_ply(self._tmp))
        call = c.calls[0]
        with zipfile.ZipFile(io.BytesIO(call["zip"])) as zf:
            names = zf.namelist()
        self.assertIn("tileset.json", names)     # ROOT level, no sub-folder
        self.assertIn("0.glb", names)
        # ...and the uploaded ZIP passes the prepared-ZIP lane's own check.
        doc = json.loads(zipfile.ZipFile(io.BytesIO(call["zip"]))
                         .read("tileset.json"))
        self.assertEqual(doc["asset"]["version"], "1.1")
        self.assertEqual(doc["root"]["refine"], "ADD")
        self.assertEqual(out["id"], 42)
        self.assertEqual(out["tileCount"], 1)
        self.assertEqual(out["pointCount"], 3)
        self.assertEqual(
            out["tilesetUrl"],
            "https://geoi.de/platform/tiles3d/services/42/tileset.json")

    def test_keep_local_sends_no_bounds_and_no_transform(self):
        c = _RecordingTiles3dClient(accept_bounds=True)
        out = tiles3d.publish_point_cloud(c, _write_ply(self._tmp))
        self.assertIsNone(c.calls[0]["bounds"])
        self.assertFalse(out["georeferenced"])
        doc = json.loads(zipfile.ZipFile(io.BytesIO(c.calls[0]["zip"]))
                         .read("tileset.json"))
        self.assertNotIn("transform", doc["root"])

    def test_georeferenced_las_sends_wgs84_bounds_when_supported(self):
        c = _RecordingTiles3dClient(accept_bounds=True)
        out = tiles3d.publish_point_cloud(c, _write_wgs84_las(self._tmp))
        b = c.calls[0]["bounds"]
        self.assertEqual(len(b), 4)               # [minLng,minLat,maxLng,maxLat]
        self.assertLess(b[0], b[2])
        self.assertLess(b[1], b[3])
        self.assertAlmostEqual(b[1], 48.2, delta=0.1)
        self.assertTrue(out["georeferenced"])
        doc = json.loads(zipfile.ZipFile(io.BytesIO(c.calls[0]["zip"]))
                         .read("tileset.json"))
        self.assertEqual(len(doc["root"]["transform"]), 16)

    def test_old_client_signature_still_publishes_without_bounds(self):
        # The CURRENT geoi_client.tiles3d_create has no bounds kwarg — the
        # lane must degrade gracefully (publish, just without bounds), never
        # crash with a TypeError.
        c = _RecordingTiles3dClient(accept_bounds=False)
        out = tiles3d.publish_point_cloud(c, _write_wgs84_las(self._tmp))
        self.assertEqual(out["id"], 42)
        self.assertIsNone(c.calls[0]["bounds"])

    def test_las_placement_local_keeps_it_identity_placed(self):
        c = _RecordingTiles3dClient(accept_bounds=True)
        out = tiles3d.publish_point_cloud(
            c, _write_wgs84_las(self._tmp), placement="local")
        self.assertFalse(out["georeferenced"])
        self.assertIsNone(c.calls[0]["bounds"])

    def test_title_defaults_to_the_file_stem(self):
        c = _RecordingTiles3dClient()
        tiles3d.publish_point_cloud(c, _write_ply(self._tmp, "My Cloud.ply"))
        self.assertEqual(c.calls[0]["title"], "My Cloud")
        self.assertEqual(c.calls[0]["filename"], "My Cloud.zip")

    def test_missing_file_fails_fast_before_any_upload(self):
        c = _RecordingTiles3dClient()
        with self.assertRaises(tiles3d.Tiles3dError):
            tiles3d.publish_point_cloud(c, os.path.join(self._tmp, "no.las"))
        self.assertEqual(c.calls, [])

    def test_decode_refusal_maps_to_tiles3d_error(self):
        path = os.path.join(self._tmp, "junk.las")
        with open(path, "wb") as fh:
            fh.write(b"not a point cloud at all")
        c = _RecordingTiles3dClient()
        with self.assertRaises(tiles3d.Tiles3dError):
            tiles3d.publish_point_cloud(c, path)
        self.assertEqual(c.calls, [])

    def test_server_error_maps_to_friendly_text(self):
        c = _RecordingTiles3dClient(
            error=GeoiError("raw", code="quota_exceeded"))
        with self.assertRaises(tiles3d.Tiles3dError) as ctx:
            tiles3d.publish_point_cloud(c, _write_ply(self._tmp))
        self.assertIn("quota", str(ctx.exception).lower())

    def test_cancel_before_upload_never_posts(self):
        c = _RecordingTiles3dClient()
        with self.assertRaises(tiles3d.Tiles3dError) as ctx:
            tiles3d.publish_point_cloud(c, _write_ply(self._tmp),
                                        is_cancelled=lambda: True)
        self.assertIn("cancel", str(ctx.exception).lower())
        self.assertEqual(c.calls, [])

    def test_progress_is_staged_and_monotonic(self):
        seen = []
        c = _RecordingTiles3dClient()
        tiles3d.publish_point_cloud(c, _write_ply(self._tmp),
                                    progress=seen.append)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[0], 0)
        self.assertEqual(seen[-1], 100)
        self.assertIn(10, seen)                   # read -> tile boundary
        self.assertIn(85, seen)                   # tile -> zip boundary

    def test_zip_is_deterministic(self):
        c1 = _RecordingTiles3dClient()
        c2 = _RecordingTiles3dClient()
        path = _write_ply(self._tmp)
        tiles3d.publish_point_cloud(c1, path)
        tiles3d.publish_point_cloud(c2, path)
        self.assertEqual(c1.calls[0]["zip"], c2.calls[0]["zip"])


# ------------------------------------------------- tasks.py additions
def _import_tasks_with_qgis_stub():
    """Import ``geoi.tasks`` with a minimal ``qgis.core`` stub (the suite's
    established convention — see test_tile_fixes._import_catalog_task)."""
    import types

    saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.core")}
    qgis = types.ModuleType("qgis")
    qgis_core = types.ModuleType("qgis.core")

    class _QgsTask:
        def __init__(self, *a, **k):
            self.progress = []

        def setProgress(self, value):
            self.progress.append(value)

        def isCanceled(self):
            return False

    qgis_core.QgsTask = _QgsTask
    qgis.core = qgis_core
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = qgis_core
    sys.modules.pop("geoi.tasks", None)
    import geoi as geoi_pkg

    if hasattr(geoi_pkg, "tasks"):
        delattr(geoi_pkg, "tasks")
    import importlib

    tasks = importlib.import_module("geoi.tasks")

    def cleanup():
        sys.modules.pop("geoi.tasks", None)
        if hasattr(geoi_pkg, "tasks"):
            delattr(geoi_pkg, "tasks")
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return tasks, cleanup


class Tiles3dPointCloudPublishTaskTest(unittest.TestCase):
    def setUp(self):
        self.tasks, self._cleanup = _import_tasks_with_qgis_stub()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)
        self._cleanup()

    def test_work_runs_the_whole_pipeline_with_staged_progress(self):
        client = _RecordingTiles3dClient(accept_bounds=True)
        path = _write_ply(self._tmp)
        task = self.tasks.Tiles3dPointCloudPublishTask(
            client, path, "Scan", "local", None, None)
        result = task.work()
        self.assertEqual(result["id"], 42)
        self.assertEqual(client.calls[0]["title"], "Scan")
        # setProgress received the pipeline's staged 0..100 callbacks.
        self.assertIn(0, task.progress)
        self.assertIn(100, task.progress)

    def test_work_passes_placement_and_reproject_fn_through(self):
        recorded = {}

        def fake_publish(client, path, title="", placement="reproject",
                         reproject_fn=None, progress=None, is_cancelled=None):
            recorded.update(placement=placement, reproject_fn=reproject_fn,
                            title=title, path=path)
            return {"id": 1}

        from geoi import tiles3d as tiles3d_mod

        saved = tiles3d_mod.publish_point_cloud
        tiles3d_mod.publish_point_cloud = fake_publish
        try:
            fn = object()
            task = self.tasks.Tiles3dPointCloudPublishTask(
                None, "/x/a.las", "T", "reproject", fn, None)
            task.work()
        finally:
            tiles3d_mod.publish_point_cloud = saved
        self.assertEqual(recorded["placement"], "reproject")
        self.assertIs(recorded["reproject_fn"], fn)
        self.assertEqual(recorded["title"], "T")

    def test_friendly_error_surfaces_verbatim_via_run(self):
        client = _RecordingTiles3dClient(
            error=GeoiError("raw", code="quota_exceeded"))
        task = self.tasks.Tiles3dPointCloudPublishTask(
            client, _write_ply(self._tmp), "", "local", None, None)
        self.assertFalse(task.run())
        self.assertIn("quota", task._error.lower())


class CatalogTaskTiles3dTest(unittest.TestCase):
    """CatalogTask fetches the 3D Tiles service list as OPTIONAL context —
    fail-soft + short timeout, stored under core['tiles3d'] for the content
    tree (the consumer lands separately)."""

    class _Client:
        def __init__(self, fail=False):
            self.fail = fail
            self.timeouts = {}

        def list_services(self):
            return []

        def list_projects(self):
            return []

        def list_folders(self):
            return []

        def list_groups(self, timeout=None):
            return []

        def tile_services(self, timeout=None):
            return []

        def storage(self, timeout=None):
            return None

        def tiles3d_list(self, timeout=None):
            self.timeouts["tiles3d"] = timeout
            if self.fail:
                raise RuntimeError("tiles3d endpoint down")
            return [{"id": 5, "title": "Scan"}]

    def setUp(self):
        self.tasks, self._cleanup = _import_tasks_with_qgis_stub()

    def tearDown(self):
        self._cleanup()

    def test_tiles3d_list_lands_in_core_with_short_timeout(self):
        client = self._Client()
        result = self.tasks.CatalogTask(client, on_done=None).work()
        self.assertEqual(result["tiles3d"], [{"id": 5, "title": "Scan"}])
        self.assertEqual(client.timeouts["tiles3d"],
                         self.tasks.CatalogTask.OPTIONAL_TIMEOUT)

    def test_tiles3d_failure_degrades_to_an_empty_bucket(self):
        client = self._Client(fail=True)
        result = self.tasks.CatalogTask(client, on_done=None).work()
        self.assertEqual(result["tiles3d"], [])
        self.assertEqual(result["services"], [])   # core load unaffected


class Tiles3dFriendlyErrorTest(unittest.TestCase):
    def test_each_known_code_maps(self):
        for code, expected in geoi_client.TILES3D_ERROR_MESSAGES.items():
            exc = GeoiError("raw", code=code)
            self.assertEqual(geoi_client.tiles3d_friendly_error(exc), expected)

    def test_shared_code_falls_back_to_raster_map(self):
        # A code only in the raster map still resolves (shared plumbing).
        exc = GeoiError("raw", code="feature_off")
        self.assertIn("administrator", geoi_client.tiles3d_friendly_error(exc))

    def test_unknown_code_falls_back_to_server_message(self):
        exc = GeoiError("Something specific.", code="brand_new_code")
        self.assertEqual(geoi_client.tiles3d_friendly_error(exc),
                         "Something specific.")


if __name__ == "__main__":
    unittest.main()
