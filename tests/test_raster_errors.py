"""Tests for the raster-publish error-code -> friendly-message map (P0).

The geoi platform answers a raster publish with a stable error CODE
(`feature_off`, `quota_exceeded`, …); `friendly_error` turns that into a
clear, actionable sentence for the QGIS publish dialog, falling back to the
server's own message for any unmapped code.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi.geoi_client import (  # noqa: E402
    RASTER_ERROR_MESSAGES,
    GeoiError,
    _error_code,
    _error_message,
    friendly_error,
)


class ServerPayloadShapeTest(unittest.TestCase):
    """The platform emits `error` as a flat STRING code (not a dict). The
    code/message extractors MUST handle that shape, or `friendly_error` never
    reaches the map and the user sees the bare `feature_off` token."""

    PAYLOAD = {
        "ok": False,
        "error": "feature_off",
        "message": ("Raster tile publishing is not enabled on this server. "
                    "Ask your administrator to configure a tile storage "
                    "backend (Account → Administration → Tile Services)."),
    }

    def test_flat_string_error_is_read_as_the_code(self):
        self.assertEqual(_error_code(self.PAYLOAD), "feature_off")

    def test_message_prefers_the_human_sentence_over_the_token(self):
        # Never echo the bare "feature_off" token when a sibling message exists.
        self.assertNotEqual(_error_message(self.PAYLOAD), "feature_off")
        self.assertIn("administrator", _error_message(self.PAYLOAD))

    def test_real_payload_through_friendly_error_hits_the_map(self):
        # The integration that was dead: a real server payload -> GeoiError
        # (as _parse builds it) -> friendly_error -> the mapped sentence.
        exc = GeoiError(
            _error_message(self.PAYLOAD), status=503,
            code=_error_code(self.PAYLOAD),
        )
        self.assertEqual(friendly_error(exc),
                         RASTER_ERROR_MESSAGES["feature_off"])

    def test_string_error_with_no_message_falls_back_to_the_code(self):
        self.assertEqual(_error_message({"error": "weird_code"}), "weird_code")
        self.assertEqual(_error_code({"error": "weird_code"}), "weird_code")


class FriendlyErrorTest(unittest.TestCase):
    def test_each_known_code_maps_to_its_friendly_message(self):
        for code, expected in RASTER_ERROR_MESSAGES.items():
            exc = GeoiError("raw server message", status=503, code=code)
            self.assertEqual(friendly_error(exc), expected)

    def test_feature_off_points_to_administration(self):
        exc = GeoiError("Raster tiles are not enabled.", code="feature_off")
        msg = friendly_error(exc)
        self.assertIn("administrator", msg)
        self.assertIn("Tile Services", msg)

    def test_unknown_code_falls_back_to_server_message(self):
        exc = GeoiError("Something specific went wrong.", code="some_new_code")
        self.assertEqual(friendly_error(exc), "Something specific went wrong.")

    def test_no_code_falls_back_to_message(self):
        exc = GeoiError("Network error: timed out.")
        self.assertEqual(friendly_error(exc), "Network error: timed out.")

    def test_plain_exception_falls_back_to_str(self):
        exc = ValueError("boom")
        self.assertEqual(friendly_error(exc), "boom")


if __name__ == "__main__":
    unittest.main()
