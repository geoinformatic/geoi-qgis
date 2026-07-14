"""Pure tests for the OGC 3D Tiles encoder (``geoi.tiles3d_encoder``) — the
pure-Python port of the geoi web encoder (src/scan3d/tiles3d-encoder.js).

No QGIS. The contract proven here mirrors the frozen validator rule set
(Tileset3DValidator R1..R13 / tiles3d-ogc-validate.js): asset.version, root
refine, boundingVolume.box shape, transform shape, non-increasing
geometricError, safe relative content uris that all resolve — plus GLB
structural validity (magic/version/4-byte alignment) and determinism
(byte-identical across runs; the committed PHP-validator fixture depends on
it)."""

import json
import math
import os
import struct
import sys
import unittest
from array import array

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import tiles3d_encoder as enc  # noqa: E402
from geoi.tiles3d_encoder import TilesetEncodeError  # noqa: E402


def _spiral_cloud(n):
    """Deterministic local Y-up test cloud (no randomness — fixture rules)."""
    pos = array("f", bytes(12 * n))
    col = bytearray(3 * n)
    for i in range(n):
        t = i / n
        a = t * 6 * math.pi
        pos[3 * i] = 12 * t * math.cos(a)
        pos[3 * i + 1] = 3 * t
        pos[3 * i + 2] = 12 * t * math.sin(a)
        col[3 * i] = (i * 7) % 256
        col[3 * i + 1] = (i * 13) % 256
        col[3 * i + 2] = (i * 29) % 256
    return {"positions": pos, "colors": col, "point_count": n}


ORIGIN = {"lat": 48.2082, "lng": 16.3738, "alt": 171.3}


def _tileset_doc(built):
    self_files = {f["path"]: f["bytes"] for f in built["files"]}
    return json.loads(self_files["tileset.json"].decode("ascii")), self_files


def _parse_glb(data):
    magic, version, total = struct.unpack_from("<III", data, 0)
    json_len, json_type = struct.unpack_from("<II", data, 12)
    doc = json.loads(data[20:20 + json_len].decode("ascii"))
    bin_off = 20 + json_len
    bin_len, bin_type = struct.unpack_from("<II", data, bin_off)
    return {"magic": magic, "version": version, "total": total,
            "json_len": json_len, "json_type": json_type, "doc": doc,
            "bin_len": bin_len, "bin_type": bin_type,
            "bin": data[bin_off + 8: bin_off + 8 + bin_len]}


class GlbStructureTest(unittest.TestCase):
    def test_magic_version_lengths_and_alignment(self):
        built = enc.build_tileset(_spiral_cloud(50))
        glb = [f for f in built["files"] if f["path"].endswith(".glb")][0]
        data = glb["bytes"]
        g = _parse_glb(data)
        self.assertEqual(g["magic"], 0x46546C67)          # 'glTF'
        self.assertEqual(g["version"], 2)
        self.assertEqual(g["total"], len(data))
        self.assertEqual(len(data) % 4, 0)
        self.assertEqual(g["json_len"] % 4, 0)
        self.assertEqual(g["json_type"], 0x4E4F534A)      # 'JSON'
        self.assertEqual(g["bin_type"], 0x004E4942)       # 'BIN\0'
        self.assertEqual(g["bin_len"] % 4, 0)

    def test_gltf_doc_shape(self):
        built = enc.build_tileset(_spiral_cloud(10))
        g = _parse_glb(built["files"][1]["bytes"])
        doc = g["doc"]
        self.assertEqual(doc["asset"]["version"], "2.0")
        self.assertIn("KHR_materials_unlit", doc["extensionsUsed"])
        prim = doc["meshes"][0]["primitives"][0]
        self.assertEqual(prim["mode"], 0)                  # POINTS
        self.assertEqual(prim["attributes"], {"POSITION": 0, "COLOR_0": 1})
        acc_pos, acc_col = doc["accessors"]
        self.assertEqual(acc_pos["componentType"], 5126)   # FLOAT
        self.assertEqual(acc_pos["type"], "VEC3")
        self.assertEqual(acc_pos["count"], 10)
        self.assertEqual(len(acc_pos["min"]), 3)
        self.assertEqual(len(acc_pos["max"]), 3)
        # COLOR_0 is VEC4 normalized UNSIGNED_BYTE (4-byte element alignment).
        self.assertEqual(acc_col["componentType"], 5121)
        self.assertEqual(acc_col["type"], "VEC4")
        self.assertTrue(acc_col["normalized"])
        # BIN payload = 12*m positions + 4*m RGBA.
        self.assertEqual(doc["buffers"][0]["byteLength"], 12 * 10 + 4 * 10)

    def test_bin_positions_and_rgba_round_trip(self):
        cloud = _spiral_cloud(4)
        built = enc.build_tileset(cloud)
        g = _parse_glb(built["files"][1]["bytes"])
        vals = struct.unpack_from("<12f", g["bin"], 0)
        self.assertEqual(list(vals), list(cloud["positions"]))
        rgba = g["bin"][48:48 + 16]
        for i in range(4):
            self.assertEqual(rgba[4 * i + 3], 255)         # alpha
            self.assertEqual(rgba[4 * i], cloud["colors"][3 * i])


class TilesetJsonTest(unittest.TestCase):
    def test_single_tile_shape_matches_the_frozen_rules(self):
        built = enc.build_tileset(_spiral_cloud(200), {"origin": ORIGIN})
        doc, files = _tileset_doc(built)
        # R1/R2 — asset.version is the string the JS emits.
        self.assertEqual(doc["asset"], {"version": "1.1"})
        # R4 — top-level geometricError finite >= 0.
        self.assertGreaterEqual(doc["geometricError"], 0)
        root = doc["root"]
        # R7 — root refine present, ADD (additive tree).
        self.assertEqual(root["refine"], "ADD")
        # R8 — box of exactly 12 finite numbers.
        box = root["boundingVolume"]["box"]
        self.assertEqual(len(box), 12)
        self.assertTrue(all(isinstance(v, (int, float))
                            and math.isfinite(v) for v in box))
        # R9 — transform 16 finite numbers; plausible ECEF translation.
        tf = root["transform"]
        self.assertEqual(len(tf), 16)
        r = math.sqrt(tf[12] ** 2 + tf[13] ** 2 + tf[14] ** 2)
        self.assertTrue(6.3e6 < r < 6.5e6,
                        "ECEF translation magnitude {} implausible".format(r))
        self.assertEqual(tf[15], 1)
        # A leaf root reports geometricError 0 (client always refines).
        self.assertEqual(root["geometricError"], 0)
        # R11/R12/R13 — safe relative uri that resolves in the file set.
        self.assertEqual(root["content"]["uri"], "0.glb")
        self.assertIn("0.glb", files)
        self.assertEqual(built["tile_count"], 1)

    def test_no_origin_means_no_transform(self):
        doc, _ = _tileset_doc(enc.build_tileset(_spiral_cloud(20)))
        self.assertNotIn("transform", doc["root"])

    def test_transform_matches_the_js_reference(self):
        # Same origin as the node-executed JS _rootTransform reference run.
        tf = enc._root_transform(ORIGIN, 0, False)
        js = [-0.28190275582883989, 0.95944298228508895, 0.0, 0.0,
              -0.71533323233889379, -0.21017862786594063,
              0.66642577313604900, 0.0,
              0.63939753124929699, 0.18786726200241746,
              0.74557138417459357, 0.0,
              4085883.7593131438, 1200511.0392317437,
              4732463.2931236019, 1.0]
        for a, b in zip(tf, js):
            self.assertLess(abs(a - b), 1e-9)

    def test_multi_tile_tree_covers_every_point_once(self):
        n = enc.SINGLE_TILE_MAX + 10000
        built = enc.build_tileset(_spiral_cloud(n))
        doc, files = _tileset_doc(built)
        self.assertGreater(built["tile_count"], 1)

        total = 0
        ge_ok = []

        def walk(tile, parent_ge):
            nonlocal total
            uri = tile["content"]["uri"]
            self.assertIn(uri, files)
            self.assertFalse(uri.startswith("/") or ".." in uri)
            g = _parse_glb(files[uri])
            total += g["doc"]["accessors"][0]["count"]
            # R6 — child geometricError <= parent geometricError.
            if parent_ge is not None:
                ge_ok.append(tile["geometricError"] <= parent_ge)
            for c in tile.get("children", []):
                walk(c, tile["geometricError"])

        walk(doc["root"], None)
        self.assertEqual(total, n, "every point lands in exactly one tile")
        self.assertTrue(all(ge_ok))
        # files[] carries the root tileset.json + one GLB per tile.
        self.assertEqual(len(built["files"]), built["tile_count"] + 1)

    def test_bounds_reports_the_local_extent(self):
        built = enc.build_tileset(_spiral_cloud(30))
        b = built["bounds"]
        self.assertEqual(len(b), 6)
        self.assertLessEqual(b[0], b[3])
        self.assertLessEqual(b[1], b[4])
        self.assertLessEqual(b[2], b[5])


class DeterminismTest(unittest.TestCase):
    def test_byte_stable_across_two_runs(self):
        a = enc.build_tileset(_spiral_cloud(300), {"origin": ORIGIN})
        b = enc.build_tileset(_spiral_cloud(300), {"origin": ORIGIN})
        self.assertEqual([f["path"] for f in a["files"]],
                         [f["path"] for f in b["files"]])
        for fa, fb in zip(a["files"], b["files"]):
            self.assertEqual(fa["bytes"], fb["bytes"], fa["path"])

    def test_module_has_no_time_or_random(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "geoi", "tiles3d_encoder.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import time", src)
        self.assertNotIn("import random", src)
        self.assertNotIn("datetime", src)

    def test_js_number_formatting_parity(self):
        cases = [(4.76837158203125e-07, "4.76837158203125e-7"),
                 (1e-05, "0.00001"), (0.0001, "0.0001"),
                 (1e21, "1e+21"), (1e16, "10000000000000000"),
                 (-1.5e-07, "-1.5e-7"), (123.456, "123.456"),
                 (-0.0, "0"), (2.5e22, "2.5e+22")]
        for v, want in cases:
            self.assertEqual(enc._js_num_str(v), want)


class StrictOrThrowTest(unittest.TestCase):
    def test_bad_clouds_throw_malformed(self):
        good = _spiral_cloud(3)
        bad_color = _spiral_cloud(3)
        bad_color["colors"] = [0, 0, 300] * 3
        bad_pos = _spiral_cloud(3)
        bad_pos["positions"] = [0.0, math.nan, 0.0] * 3
        cases = [
            None,
            {},
            {"positions": good["positions"], "colors": good["colors"],
             "point_count": 0},
            {"positions": good["positions"][:6], "colors": good["colors"],
             "point_count": 3},
            {"positions": good["positions"], "colors": good["colors"][:6],
             "point_count": 3},
            bad_color,
            bad_pos,
        ]
        for cloud in cases:
            with self.assertRaises(TilesetEncodeError):
                enc.build_tileset(cloud)

    def test_error_is_flagged_malformed(self):
        try:
            enc.build_tileset({})
        except TilesetEncodeError as exc:
            self.assertTrue(exc.malformed)
        else:
            self.fail("expected TilesetEncodeError")

    def test_bad_origin_throws(self):
        cloud = _spiral_cloud(3)
        with self.assertRaises(TilesetEncodeError):
            enc.build_tileset(cloud, {"origin": {"lat": 91, "lng": 0}})
        with self.assertRaises(TilesetEncodeError):
            enc.build_tileset(cloud, {"origin": {"lat": math.nan, "lng": 0}})

    def test_bad_max_depth_throws(self):
        with self.assertRaises(TilesetEncodeError):
            enc.build_tileset(_spiral_cloud(3), {"max_depth": -1})


if __name__ == "__main__":
    unittest.main()
