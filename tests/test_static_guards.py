"""Static guards that lock in hard-won bug fixes (no QGIS needed)."""

import os
import unittest

_PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "geoi")


def _read(*parts):
    with open(os.path.join(_PLUGIN, *parts), encoding="utf-8") as fh:
        return fh.read()


class CrsRegressionTest(unittest.TestCase):
    """The Feature Service is Web Mercator (3857). Adding a layer as a
    hardcoded EPSG:4326 silently corrupts edits on commit and misplaces the
    extent — never reintroduce a literal 4326 layer CRS in the add path."""

    def test_add_service_does_not_hardcode_4326(self):
        src = _read("plugin.py")
        self.assertNotIn("crs='EPSG:4326'", src)
        self.assertNotIn('crs="EPSG:4326"', src)
        # the add path must derive the CRS from the service metadata
        self.assertIn("epsg_from_spatial_reference", src)

    def test_add_service_refreshes_canvas(self):
        # the layers must be framed/repainted so they don't only appear on pan
        src = _read("plugin.py")
        self.assertIn("_zoom_to_layers", src)
        self.assertIn(".refresh()", src)


class MetadataCompatibilityTest(unittest.TestCase):
    """Without qgisMaximumVersion, QGIS caps the supported range at 3.99 and
    refuses to load the plugin on QGIS 4.x ("incompatible with this version of
    QGIS"). The plugin must declare it works on QGIS 3 AND 4."""

    def _meta(self):
        out = {}
        for line in _read("metadata.txt").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
        return out

    def test_declares_qgis4_maximum(self):
        meta = self._meta()
        self.assertIn("qgisMaximumVersion", meta)
        major = int(meta["qgisMaximumVersion"].split(".")[0])
        self.assertGreaterEqual(major, 4, "must support QGIS 4.x")

    def test_minimum_is_qgis3(self):
        meta = self._meta()
        self.assertIn("qgisMinimumVersion", meta)
        self.assertEqual(meta["qgisMinimumVersion"].split(".")[0], "3")


if __name__ == "__main__":
    unittest.main()
