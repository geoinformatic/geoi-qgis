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


class FeedbackFeatureTest(unittest.TestCase):
    """The Send feedback feature: a Web-menu action, a client POST to the
    platform's /feedback endpoint with proper multipart text fields, and a
    transparent system-info collector. Optional sign-in (works signed out)."""

    def test_menu_action_and_handler(self):
        src = _read("plugin.py")
        self.assertIn("Send feedback", src)
        self.assertIn("def open_feedback", src)
        self.assertIn("def _system_info", src)

    def test_client_posts_to_feedback_endpoint(self):
        src = _read("geoi_client.py")
        self.assertIn("def send_feedback", src)
        self.assertIn('"/feedback"', src)
        # text fields must be plain form parts (land in PHP $_POST), so a
        # dedicated form encoder — NOT the file-only encode_multipart — is used
        self.assertIn("def encode_multipart_form", src)

    def test_feedback_uses_exec_not_exec_underscore(self):
        # Qt6 parity: the dialog must use .exec(), never .exec_()
        src = _read("plugin.py")
        self.assertIn("dlg.exec()", src)
        self.assertNotIn("dlg.exec_(", src)


class RasterCrsForcedTest(unittest.TestCase):
    """#552: cloud-native raster tiles are Web Mercator (EPSG:3857), FORCED.
    There is no CRS parameter, no override, and no CRS-selection widget — a
    non-3857 dstSRS or a CRS picker would let a user publish tiles the geoi
    web client cannot consume."""

    import re as _re

    def test_raster_dst_srs_is_only_ever_3857(self):
        src = _read("raster.py")
        # Every dstSRS= assignment must be the TARGET_CRS constant (or the
        # literal EPSG:3857) — never any other CRS slipped in.
        for match in self._re.finditer(r"dstSRS\s*=\s*([^,\n)]+)", src):
            value = match.group(1).strip()
            self.assertIn(
                value, ("TARGET_CRS", '"EPSG:3857"', "'EPSG:3857'"),
                "raster.py dstSRS must be EPSG:3857/TARGET_CRS, got " + value,
            )
        # And the constant itself is Web Mercator.
        self.assertIn('TARGET_CRS = "EPSG:3857"', src)

    def test_publish_raster_dialog_has_no_crs_selection_widget(self):
        src = _read("gui", "dialogs.py")
        # Isolate the PublishRasterDialog body so the guard is scoped to it.
        start = src.index("class PublishRasterDialog")
        nxt = src.find("\nclass ", start + 1)
        body = src[start: nxt if nxt != -1 else len(src)]
        for widget in (
            "QgsProjectionSelectionWidget",
            "QgsProjectionSelectionTreeWidget",
            "QgsCoordinateReferenceSystem",
            "setCrs(",
            "QgsCrsSelectionWidget",
        ):
            self.assertNotIn(widget, body,
                             "PublishRasterDialog must not offer a CRS picker")
        # The CRS is communicated read-only as the fixed Web Mercator label.
        self.assertIn("EPSG:3857", body)
        self.assertIn("fixed", body)


class PresignedUrlNotLoggedTest(unittest.TestCase):
    """The presigned PUT URL carries the SigV4 signature (a short-lived write
    credential) and must never be logged verbatim — only a redacted form."""

    def test_put_url_logs_redacted_url(self):
        src = _read("geoi_client.py")
        body = src[src.index("def put_url"):]
        body = body[: body.find("\n    def ", 1) if body.find("\n    def ", 1) != -1 else len(body)]
        # The raw `url` must not be handed straight to the logger.
        self.assertNotIn('_note("PUT {} ({} bytes) -> HTTP {}".format(url,', body,
                         "put_url must log a redacted URL, not the signed one")
        # It strips the query string (where the signature lives) before logging.
        self.assertIn('url.split("?", 1)[0]', body)


if __name__ == "__main__":
    unittest.main()
