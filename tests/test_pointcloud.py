"""Pure tests for the point-cloud readers + CRS handling (``geoi.pointcloud``)
— the pure-Python port of the geoi web import lane (laz-export.js decode /
ply-export.js decode / las-crs.js / pointcloud-import.js toCloud).

No QGIS. The LAS/PLY bytes are built IN-TEST (a minimal ASPRS LAS 1.4 / PLY
writer below), so the reader round-trip is proven against independently
constructed files, not against the reader's own output. The UTM inverse is
asserted against reference values computed by executing the ORIGINAL JS
``Scan3DLasCrs.utmToWgs84`` (src/scan3d/las-crs.js) — agreement to 1e-9 deg.
"""

import math
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import pointcloud  # noqa: E402
from geoi.pointcloud import PointCloudError  # noqa: E402


# ------------------------------------------------------------ LAS builder
WGS84_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",'
    '6378137,298.257223563]],PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]'
)


def make_las(points, colors=None, intensities=None, epsg_geokey=None,
             wkt=None, scale=(1e-7, 1e-7, 0.001)):
    """A minimal ASPRS LAS 1.4 writer (PF7 with colors, PF1 without) —
    independent of the module under test. ``points`` = [(x, y, z), ...]."""
    n = len(points)
    has_rgb = colors is not None
    fmt_id, pdr_len = (7, 36) if has_rgb else (1, 28)

    vlrs = b""
    vlr_count = 0

    def _vlr(record_id, payload, desc):
        head = struct.pack("<H", 0)
        head += b"LASF_Projection".ljust(16, b"\x00")
        head += struct.pack("<HH", record_id, len(payload))
        head += desc.ljust(32, b"\x00")
        return head + payload

    if wkt is not None:
        vlrs += _vlr(2112, wkt.encode("ascii") + b"\x00", b"WKT")
        vlr_count += 1
    if epsg_geokey is not None:
        keys = struct.pack("<4H", 1, 1, 0, 1)
        keys += struct.pack("<4H", 3072, 0, 1, epsg_geokey)
        vlrs += _vlr(34735, keys, b"GeoKeys")
        vlr_count += 1

    xs, ys, zs = scale
    minx = min(p[0] for p in points)
    miny = min(p[1] for p in points)
    minz = min(p[2] for p in points)
    maxx = max(p[0] for p in points)
    maxy = max(p[1] for p in points)
    maxz = max(p[2] for p in points)
    xo, yo, zo = math.floor(minx), math.floor(miny), math.floor(minz)

    offset_to_point = 375 + len(vlrs)
    head = bytearray()
    head += b"LASF"
    head += struct.pack("<HH", 0, 0x11)          # source id, global encoding
    head += b"\x00" * 16                          # project GUID
    head += bytes((1, 4))                         # version 1.4
    head += b"test".ljust(32, b"\x00")            # system identifier
    head += b"unit-test".ljust(32, b"\x00")       # generating software
    head += struct.pack("<HHH", 0, 0, 375)        # day, year, header size
    head += struct.pack("<II", offset_to_point, vlr_count)
    head += struct.pack("<BH", fmt_id, pdr_len)
    head += struct.pack("<I", n)                  # legacy point count
    head += struct.pack("<5I", n, 0, 0, 0, 0)     # legacy by return
    head += struct.pack("<6d", xs, ys, zs, xo, yo, zo)
    head += struct.pack("<6d", maxx, minx, maxy, miny, maxz, minz)
    head += struct.pack("<QQI", 0, 0, 0)          # waveform, EVLR start/count
    head += struct.pack("<Q", n)                  # 1.4 point count
    head += struct.pack("<Q", n) + struct.pack("<14Q", *([0] * 14))
    assert len(head) == 375, len(head)

    recs = bytearray()
    for i, (x, y, z) in enumerate(points):
        recs += struct.pack("<iii",
                            round((x - xo) / xs),
                            round((y - yo) / ys),
                            round((z - zo) / zs))
        recs += struct.pack("<H", intensities[i] if intensities else 0)
        if has_rgb:
            recs += struct.pack("<BBBB", 0x11, 0, 0, 0)   # ret, flags, cls, ud
            recs += struct.pack("<hH", 0, 0)              # scan angle, psid
            recs += struct.pack("<d", 0.0)                # gps
            r, g, b = colors[i]
            recs += struct.pack("<HHH", r * 257, g * 257, b * 257)
        else:
            recs += struct.pack("<BBbB", 0x11, 0, 0, 0)   # ret, cls, angle, ud
            recs += struct.pack("<H", 0)                  # point source id
            recs += struct.pack("<d", 0.0)                # gps
    return bytes(head) + vlrs + bytes(recs)


def make_ply(points, colors=None, fmt="binary_little_endian"):
    """A minimal Stanford PLY writer (float32 x/y/z + uchar rgb)."""
    n = len(points)
    header = ["ply", "format {} 1.0".format(fmt), "element vertex " + str(n),
              "property float x", "property float y", "property float z"]
    if colors is not None:
        header += ["property uchar red", "property uchar green",
                   "property uchar blue"]
    header.append("end_header")
    head = ("\n".join(header) + "\n").encode("ascii")
    body = bytearray()
    if fmt == "ascii":
        lines = []
        for i, (x, y, z) in enumerate(points):
            parts = [repr(float(x)), repr(float(y)), repr(float(z))]
            if colors is not None:
                parts += [str(c) for c in colors[i]]
            lines.append(" ".join(parts))
        body = ("\n".join(lines) + "\n").encode("ascii")
    else:
        e = "<" if fmt == "binary_little_endian" else ">"
        for i, (x, y, z) in enumerate(points):
            body += struct.pack(e + "fff", x, y, z)
            if colors is not None:
                body += struct.pack("BBB", *colors[i])
    return head + bytes(body)


def _wgs84_points(n=50):
    """A deterministic small WGS84 cluster near Vienna."""
    pts, cols = [], []
    for i in range(n):
        t = i / n
        a = t * 4 * math.pi
        pts.append((16.3738 + 0.0004 * t * math.cos(a),
                    48.2082 + 0.0003 * t * math.sin(a),
                    171.3 + 5 * t))
        cols.append(((i * 17) % 256, (i * 23) % 256, (i * 31) % 256))
    return pts, cols


# ------------------------------------------------------------------ LAS
class ReadLasTest(unittest.TestCase):
    def test_pf7_round_trip_positions_colors_crs(self):
        pts, cols = _wgs84_points(40)
        las = make_las(pts, colors=cols, wkt=WGS84_WKT)
        out = pointcloud.read_las(las)
        self.assertEqual(out["point_count"], 40)
        self.assertEqual(len(out["positions"]), 120)
        for i, (x, y, z) in enumerate(pts):
            self.assertAlmostEqual(out["positions"][3 * i], x, places=6)
            self.assertAlmostEqual(out["positions"][3 * i + 1], y, places=6)
            self.assertAlmostEqual(out["positions"][3 * i + 2], z, places=3)
        for i, c in enumerate(cols):
            self.assertEqual(tuple(out["colors"][3 * i:3 * i + 3]), c)
        # The WKT VLR yields the CRS hint with the OUTERMOST authority code.
        self.assertEqual(out["crs"]["epsg"], 4326)
        self.assertIn("WGS 84", out["crs"]["wkt"])

    def test_pf1_has_no_colors_but_reads_intensity(self):
        pts = [(16.0, 48.0, 100.0), (16.001, 48.001, 101.0)]
        las = make_las(pts, intensities=[7, 65535])
        out = pointcloud.read_las(las)
        self.assertIsNone(out["colors"])
        self.assertEqual(list(out["intensities"]), [7, 65535])

    def test_geokey_vlr_yields_epsg(self):
        pts = [(500000.0, 5400000.0, 10.0), (500001.0, 5400001.0, 11.0)]
        las = make_las(pts, epsg_geokey=32633, scale=(0.001, 0.001, 0.001))
        out = pointcloud.read_las(las)
        self.assertEqual(out["crs"]["epsg"], 32633)

    def test_truncated_point_block_is_malformed(self):
        pts, cols = _wgs84_points(10)
        las = make_las(pts, colors=cols)
        with self.assertRaises(PointCloudError) as ctx:
            pointcloud.read_las(las[:-8])
        self.assertTrue(ctx.exception.malformed)

    def test_forged_vlr_overrun_is_malformed(self):
        pts, cols = _wgs84_points(4)
        las = bytearray(make_las(pts, colors=cols, wkt=WGS84_WKT))
        # Forge a huge VLR count — must be bounded, never a long loop/read.
        struct.pack_into("<I", las, 100, 4000000000)
        with self.assertRaises(PointCloudError) as ctx:
            pointcloud.read_las(bytes(las))
        self.assertTrue(ctx.exception.malformed)

    def test_not_las_is_malformed(self):
        with self.assertRaises(PointCloudError):
            pointcloud.read_las(b"NOPE" + b"\x00" * 400)


# ------------------------------------------------------------------ PLY
class ReadPlyTest(unittest.TestCase):
    def test_binary_le_round_trip(self):
        pts = [(1.5, -2.25, 3.0), (0.0, 0.5, -1.0), (10.0, 20.0, 30.0)]
        cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        out = pointcloud.read_ply(make_ply(pts, cols))
        self.assertEqual(out["point_count"], 3)
        for i, (x, y, z) in enumerate(pts):
            self.assertAlmostEqual(out["positions"][3 * i], x, places=6)
            self.assertAlmostEqual(out["positions"][3 * i + 1], y, places=6)
            self.assertAlmostEqual(out["positions"][3 * i + 2], z, places=6)
        for i, c in enumerate(cols):
            self.assertEqual(tuple(out["colors"][3 * i:3 * i + 3]), c)
        self.assertEqual(out["bounds"]["min"], [0.0, -2.25, -1.0])

    def test_ascii_round_trip_without_colors(self):
        pts = [(0.5, 1.5, 2.5), (-1.0, -2.0, -3.0)]
        out = pointcloud.read_ply(make_ply(pts, None, fmt="ascii"))
        self.assertEqual(out["point_count"], 2)
        self.assertIsNone(out["colors"])
        self.assertAlmostEqual(out["positions"][3], -1.0, places=6)

    def test_overclaimed_vertex_count_is_malformed(self):
        ply = bytearray(make_ply([(1.0, 2.0, 3.0)], [(9, 9, 9)]))
        forged = ply.replace(b"element vertex 1", b"element vertex 9")
        with self.assertRaises(PointCloudError) as ctx:
            pointcloud.read_ply(bytes(forged))
        self.assertTrue(ctx.exception.malformed)

    def test_hostile_property_type_is_malformed(self):
        ply = make_ply([(1.0, 2.0, 3.0)], None).replace(
            b"property float x", b"property toString x")
        with self.assertRaises(PointCloudError) as ctx:
            pointcloud.read_ply(ply)
        self.assertTrue(ctx.exception.malformed)

    def test_non_finite_ascii_color_degrades_to_zero(self):
        # A hostile 'nan' colour token must degrade like the JS Uint8Array
        # store (NaN -> 0), never crash with a bare ValueError.
        ply = make_ply([(1.0, 2.0, 3.0)], [(7, 8, 9)], fmt="ascii").replace(
            b" 7 8 9", b" nan inf 9")
        out = pointcloud.read_ply(ply)
        self.assertEqual(tuple(out["colors"][0:3]), (0, 255, 9))


# ------------------------------------------------------------ CRS detect
class DetectCrsTest(unittest.TestCase):
    def test_epsg_matrix(self):
        self.assertEqual(pointcloud.detect_crs({"epsg": 4326, "wkt": None},
                                               None), {"kind": "wgs84"})
        self.assertEqual(pointcloud.detect_crs({"epsg": 4979, "wkt": None},
                                               None), {"kind": "wgs84"})
        det = pointcloud.detect_crs({"epsg": 32633, "wkt": None}, None)
        self.assertEqual((det["kind"], det["zone"], det["hemisphere"]),
                         ("utm", 33, "N"))
        det = pointcloud.detect_crs({"epsg": 32756, "wkt": None}, None)
        self.assertEqual((det["kind"], det["zone"], det["hemisphere"]),
                         ("utm", 56, "S"))
        det = pointcloud.detect_crs({"epsg": 25832, "wkt": None}, None)
        self.assertEqual((det["kind"], det["zone"], det["hemisphere"]),
                         ("utm", 32, "N"))

    def test_wkt_zone_regex(self):
        det = pointcloud.detect_crs(
            {"epsg": None, "wkt": 'PROJCS["WGS 84 / UTM zone 33N",...]'}, None)
        self.assertEqual((det["kind"], det["zone"], det["hemisphere"]),
                         ("utm", 33, "N"))

    def test_unsupported_epsg_carries_reason(self):
        det = pointcloud.detect_crs({"epsg": 2154, "wkt": None}, None)
        self.assertEqual(det["kind"], "unsupported")
        self.assertIn("2154", det["reason"])

    def test_no_hint_bounds_heuristic(self):
        geo = {"min_lng": -10, "max_lng": 10, "min_lat": 40, "max_lat": 50}
        self.assertEqual(pointcloud.detect_crs(None, geo), {"kind": "wgs84"})
        proj = {"min_lng": 300000, "max_lng": 301000,
                "min_lat": 5400000, "max_lat": 5401000}
        self.assertEqual(pointcloud.detect_crs(None, proj)["kind"],
                         "unsupported")


class UtmToWgs84Test(unittest.TestCase):
    # Reference values computed by EXECUTING the original JS implementation
    # (src/scan3d/las-crs.js utmToWgs84) under node — full double precision.
    # The Python port must agree to ~1e-9 degrees (it is in fact bit-exact).
    JS_REFERENCE = [
        (500000.0, 0.0, 31, "N", 0.0, 3.0000000000000004),
        (630084.0, 4833438.0, 17, "N",
         43.642561780309315, -79.387142869490873),
        (448251.795, 5411932.678, 31, "N",
         48.858200001898524, 2.2944999971652766),
        (334897.512, 6252442.981, 56, "S",
         -33.855409194099067, 151.21529588961553),
        (291021.5, 5744128.25, 33, "N",
         51.808876650067326, 11.968468620005941),
    ]

    def test_agrees_with_the_js_reference_to_1e9(self):
        for e, n, zone, hemi, ref_lat, ref_lng in self.JS_REFERENCE:
            lat, lng = pointcloud.utm_to_wgs84(e, n, zone, hemi)
            self.assertLess(abs(lat - ref_lat), 1e-9,
                            "lat drift for zone {}{}".format(zone, hemi))
            self.assertLess(abs(lng - ref_lng), 1e-9,
                            "lng drift for zone {}{}".format(zone, hemi))

    def test_equator_at_the_central_meridian(self):
        for zone in (1, 17, 31, 32, 56, 60):
            lon0 = (zone - 1) * 6 - 180 + 3
            lat, lng = pointcloud.utm_to_wgs84(500000, 0, zone, "N")
            self.assertLess(abs(lat), 1e-9)
            self.assertLess(abs(lng - lon0), 1e-9)

    def test_malformed_arguments_throw(self):
        for bad in ((math.nan, 0, 32, "N"), (500000, 0, 0, "N"),
                    (500000, 0, 61, "N"), (500000, 0, 32, "X")):
            with self.assertRaises(PointCloudError):
                pointcloud.utm_to_wgs84(*bad)


# ---------------------------------------------------------------- to_cloud
class ToCloudTest(unittest.TestCase):
    def test_wgs84_las_is_georeferenced_at_the_centroid(self):
        pts, cols = _wgs84_points(60)
        las = make_las(pts, colors=cols, wkt=WGS84_WKT)
        res = pointcloud.to_cloud(las, "a.las")
        self.assertEqual(res["cloud"]["point_count"], 60)
        self.assertIsNotNone(res["origin"])
        self.assertAlmostEqual(res["origin"]["lat"], 48.2082, places=3)
        self.assertAlmostEqual(res["origin"]["lng"], 16.3738, places=3)
        # bounds = WGS84 [min_lng, min_lat, max_lng, max_lat]
        b = res["bounds"]
        self.assertEqual(len(b), 4)
        self.assertLess(b[0], b[2])
        self.assertLess(b[1], b[3])
        self.assertEqual(res["crs"]["source"], "wgs84")
        # local Y-up metres, centred near the origin: small magnitudes
        self.assertLess(max(abs(v) for v in res["cloud"]["positions"]), 200)

    def test_utm_las_reprojects_through_the_zone(self):
        # Vienna in UTM 33N (approx) — a tight cluster; the origin must come
        # back in geographic range near 48.2N/16.37E.
        pts = [(602327.0 + i, 5340222.0 + i, 200.0 + i) for i in range(8)]
        las = make_las(pts, colors=[(1, 2, 3)] * 8, epsg_geokey=32633,
                       scale=(0.001, 0.001, 0.001))
        res = pointcloud.to_cloud(las, "utm.las")
        self.assertEqual(res["crs"]["source"], "utm")
        self.assertEqual(res["crs"]["zone"], 33)
        self.assertAlmostEqual(res["origin"]["lat"], 48.2, delta=0.2)
        self.assertAlmostEqual(res["origin"]["lng"], 16.4, delta=0.2)

    def test_local_placement_gives_origin_none_for_any_crs(self):
        # An UNSUPPORTED CRS (Lambert-93) still imports with 'local'.
        pts = [(700000.0 + i, 6600000.0 + i, 50.0) for i in range(5)]
        las = make_las(pts, colors=[(9, 9, 9)] * 5, epsg_geokey=2154,
                       scale=(0.001, 0.001, 0.001))
        res = pointcloud.to_cloud(las, "l93.las", placement="local")
        self.assertIsNone(res["origin"])
        self.assertIsNone(res["bounds"])
        # centred on the centroid
        pos = res["cloud"]["positions"]
        n = res["cloud"]["point_count"]
        cx = sum(pos[3 * i] for i in range(n)) / n
        self.assertAlmostEqual(cx, 0.0, places=3)

    def test_unsupported_crs_with_reproject_raises_naming_qgis(self):
        pts = [(700000.0, 6600000.0, 50.0), (700001.0, 6600001.0, 51.0)]
        las = make_las(pts, colors=[(9, 9, 9)] * 2, epsg_geokey=2154,
                       scale=(0.001, 0.001, 0.001))
        with self.assertRaises(PointCloudError) as ctx:
            pointcloud.to_cloud(las, "l93.las")
        self.assertTrue(ctx.exception.unsupported)
        self.assertIn("QGIS", str(ctx.exception))
        self.assertIn("2154", str(ctx.exception))

    def test_reproject_fn_injection_handles_an_arbitrary_crs(self):
        # A fake QgsCoordinateTransform-backed callable: shift the projected
        # coords into a known WGS84 spot. It must be used for EVERY point and
        # the result georeferenced there.
        pts = [(700000.0 + i, 6600000.0 + i, 50.0) for i in range(4)]
        las = make_las(pts, colors=[(1, 1, 1)] * 4, epsg_geokey=2154,
                       scale=(0.001, 0.001, 0.001))
        seen = []

        def fake_transform(x, y):
            seen.append((x, y))
            return (48.0 + (y - 6600000.0) * 1e-5,
                    16.0 + (x - 700000.0) * 1e-5)

        res = pointcloud.to_cloud(las, "l93.las", reproject_fn=fake_transform)
        self.assertEqual(len(seen), 4)
        self.assertAlmostEqual(res["origin"]["lat"], 48.0, places=4)
        self.assertAlmostEqual(res["origin"]["lng"], 16.0, places=4)
        self.assertEqual(res["crs"]["source"], "custom")

    def test_ply_takes_the_local_lane(self):
        pts = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]
        res = pointcloud.to_cloud(make_ply(pts, [(1, 2, 3)] * 3), "x.ply")
        self.assertIsNone(res["origin"])
        self.assertIsNone(res["bounds"])
        self.assertEqual(res["cloud"]["point_count"], 3)

    def test_uncolored_las_gets_intensity_turbo_ramp(self):
        pts = [(16.0, 48.0, 100.0), (16.0001, 48.0001, 101.0),
               (16.0002, 48.0002, 102.0)]
        las = make_las(pts, intensities=[0, 30000, 65535], wkt=WGS84_WKT)
        res = pointcloud.to_cloud(las, "i.las")
        cols = res["cloud"]["colors"]
        # A varying intensity yields a VARYING ramp, not flat grey.
        self.assertNotEqual(bytes(cols[0:3]), bytes(cols[6:9]))

    def test_uncolored_flat_intensity_gets_grey(self):
        pts = [(16.0, 48.0, 100.0), (16.0001, 48.0001, 101.0)]
        las = make_las(pts, intensities=[5, 5], wkt=WGS84_WKT)
        res = pointcloud.to_cloud(las, "g.las")
        self.assertEqual(list(res["cloud"]["colors"]),
                         [pointcloud.DEFAULT_GREY] * 6)

    def test_unrecognised_bytes_are_refused(self):
        with self.assertRaises(PointCloudError) as ctx:
            pointcloud.to_cloud(b"GIF89a not a point cloud", "x.gif")
        self.assertTrue(ctx.exception.unsupported)

    def test_bad_placement_is_rejected(self):
        with self.assertRaises(PointCloudError):
            pointcloud.to_cloud(b"ply\n", "x.ply", placement="teleport")


class LazLadderTest(unittest.TestCase):
    """The .laz ladder: laspy -> pdal -> a FRIENDLY error (never a stack
    trace). The rungs are monkeypatched — CI has neither laspy nor pdal."""

    def _laz_bytes(self):
        pts, cols = _wgs84_points(6)
        las = bytearray(make_las(pts, colors=cols, wkt=WGS84_WKT))
        las[104] |= 0x80          # the LASzip compression bit
        return bytes(las), bytes(bytearray(las[:104]) + b"\x07"
                                 + las[105:])   # (laz, plain las)

    def test_no_backend_yields_the_friendly_error(self):
        laz, _ = self._laz_bytes()
        saved = (pointcloud._laz_via_laspy, pointcloud._laz_via_pdal)
        pointcloud._laz_via_laspy = lambda data: None
        pointcloud._laz_via_pdal = lambda data: None
        try:
            with self.assertRaises(PointCloudError) as ctx:
                pointcloud.to_cloud(laz, "c.laz")
        finally:
            (pointcloud._laz_via_laspy, pointcloud._laz_via_pdal) = saved
        msg = str(ctx.exception)
        self.assertIn("Decompress the .laz to .las first", msg)
        self.assertIn("laspy", msg)

    def test_first_working_rung_wins(self):
        laz, plain = self._laz_bytes()
        calls = []
        saved = (pointcloud._laz_via_laspy, pointcloud._laz_via_pdal)
        pointcloud._laz_via_laspy = (
            lambda data: calls.append("laspy") or plain)
        pointcloud._laz_via_pdal = (
            lambda data: calls.append("pdal") or plain)
        try:
            res = pointcloud.to_cloud(laz, "c.laz")
        finally:
            (pointcloud._laz_via_laspy, pointcloud._laz_via_pdal) = saved
        self.assertEqual(calls, ["laspy"])      # pdal never probed
        self.assertEqual(res["cloud"]["point_count"], 6)

    def test_pdal_rung_is_used_when_laspy_is_absent(self):
        laz, plain = self._laz_bytes()
        saved = (pointcloud._laz_via_laspy, pointcloud._laz_via_pdal)
        pointcloud._laz_via_laspy = lambda data: None
        pointcloud._laz_via_pdal = lambda data: plain
        try:
            res = pointcloud.to_cloud(laz, "c.laz")
        finally:
            (pointcloud._laz_via_laspy, pointcloud._laz_via_pdal) = saved
        self.assertEqual(res["cloud"]["point_count"], 6)


class CapPointsTest(unittest.TestCase):
    """The downsample cap (voxel grid-average, then deterministic truncate)
    — proven on a tiny cap so the test stays fast."""

    def test_cap_reduces_and_is_deterministic(self):
        import random
        rng = random.Random(42)   # test-only synthesis; the module has none
        pts = [(rng.uniform(0, 10), rng.uniform(0, 10), rng.uniform(0, 10))
               for _ in range(500)]
        cloud = {
            "positions": [c for p in pts for c in p],
            "colors": bytearray(500 * 3),
            "point_count": 500,
            "bounds": pointcloud._min_max(
                [c for p in pts for c in p], 500),
        }
        a = pointcloud._cap_points(dict(cloud), 100)
        b = pointcloud._cap_points(dict(cloud), 100)
        self.assertLessEqual(a["point_count"], 100)
        self.assertEqual(list(a["positions"]), list(b["positions"]))

    def test_under_cap_is_untouched(self):
        cloud = {"positions": [0.0, 0.0, 0.0], "colors": bytearray(3),
                 "point_count": 1,
                 "bounds": {"min": [0, 0, 0], "max": [0, 0, 0]}}
        self.assertIs(pointcloud._cap_points(cloud, 10), cloud)


if __name__ == "__main__":
    unittest.main()
