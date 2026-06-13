import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import convert  # noqa: E402


class GeometryTypeTest(unittest.TestCase):
    def test_maps_all_families(self):
        self.assertEqual(convert.geoi_geometry_name("Point"), "Point")
        self.assertEqual(convert.geoi_geometry_name("MultiPoint"), "Point")
        self.assertEqual(convert.geoi_geometry_name("LineString"), "LineString")
        self.assertEqual(convert.geoi_geometry_name("MultiLineString"), "LineString")
        self.assertEqual(convert.geoi_geometry_name("CompoundCurve"), "LineString")
        self.assertEqual(convert.geoi_geometry_name("Polygon"), "Polygon")
        self.assertEqual(convert.geoi_geometry_name("MultiPolygon"), "Polygon")
        self.assertEqual(convert.geoi_geometry_name("CurvePolygon"), "Polygon")
        self.assertEqual(convert.geoi_geometry_name(""), "Point")


class GeometryCodeTest(unittest.TestCase):
    def test_maps_qgis_geometry_codes(self):
        self.assertEqual(convert.geoi_geometry_from_code(0), "Point")
        self.assertEqual(convert.geoi_geometry_from_code(1), "LineString")
        self.assertEqual(convert.geoi_geometry_from_code(2), "Polygon")
        self.assertEqual(convert.geoi_geometry_from_code(3), "Point")  # unknown -> default

    def test_accepts_enum_like(self):
        class _E:
            def __index__(self):
                return 2

            def __int__(self):
                return 2

        self.assertEqual(convert.geoi_geometry_from_code(_E()), "Polygon")


class FieldTypeTest(unittest.TestCase):
    def test_numeric_to_double_else_text(self):
        for n in ("int", "Int", "double", "LongLong", "float", "uint"):
            self.assertEqual(convert.geoi_field_type(n), "double")
        for n in ("String", "text", "Date", "Bool", ""):
            self.assertEqual(convert.geoi_field_type(n), "text")


class FeatureCollectionTest(unittest.TestCase):
    def test_shape_matches_geoi_model(self):
        layers = [{"id": "l1", "name": "Pts", "type": "Point"}]
        feats = [{"type": "Feature", "geometry": None, "properties": {"_layerId": "l1"}}]
        fc = convert.build_feature_collection(layers, feats, project_name="P", project_note="n")
        self.assertEqual(fc["type"], "FeatureCollection")
        self.assertEqual(fc["features"], feats)
        meta = fc["project_metadata"]
        self.assertEqual(meta["projectName"], "P")
        self.assertEqual(meta["projectNote"], "n")
        self.assertEqual(meta["layers"], layers)
        self.assertEqual(meta["layerGroups"], [])
        self.assertEqual(meta["plugins"], {})

    def test_new_layer_id_unique(self):
        self.assertNotEqual(convert.new_layer_id(), convert.new_layer_id())


if __name__ == "__main__":
    unittest.main()
