"""Storage overview (#677): the client getter, the byte formatter and the
overview-line builder. Pure stdlib — no QGIS GUI."""

import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi.geoi_client import (  # noqa: E402
    GeoiClient,
    GeoiError,
    fmt_bytes,
    storage_overview,
)


class FakeResponse(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(payload if isinstance(payload, bytes) else payload.encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
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
        return FakeResponse(payload, status)


def client(responses, token="T"):
    return GeoiClient(
        base_url="https://geoi.de", token=token, opener=FakeOpener(responses)
    )


ENVELOPE = {
    "ok": True,
    "usedBytes": 2_500_000_000,
    "quotaBytes": 10_000_000_000,
    "percent": 25,
    "breakdown": {
        "feature": 1_000_000_000,
        "tile": 1_200_000_000,
        "project": 200_000_000,
        "app": 100_000_000,
    },
}


class StorageClientTest(unittest.TestCase):
    def test_builds_get_hub_storage_with_bearer(self):
        c = client([(json.dumps(ENVELOPE), 200)])
        c.storage()
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.endswith("/platform/hub/storage"))
        # The signed-in bearer rides via _request.
        self.assertEqual(
            dict(req.header_items()).get("Authorization"), "Bearer T"
        )

    def test_parses_envelope(self):
        c = client([(json.dumps(ENVELOPE), 200)])
        res = c.storage()
        self.assertEqual(res["usedBytes"], 2_500_000_000)
        self.assertEqual(res["quotaBytes"], 10_000_000_000)
        self.assertEqual(res["percent"], 25)
        self.assertEqual(res["breakdown"]["tile"], 1_200_000_000)

    def test_failed_storage_is_fail_soft_for_callers(self):
        # A 500 / missing endpoint raises GeoiError — the caller (CatalogTask /
        # panel) catches it and hides the line; sign-in is never blocked.
        c = client([(json.dumps({"error": "nope"}), 500)])
        with self.assertRaises(GeoiError):
            c.storage()


class FmtBytesTest(unittest.TestCase):
    def test_renders_units(self):
        self.assertEqual(fmt_bytes(0), "0 B")
        self.assertEqual(fmt_bytes(512), "512 B")
        self.assertEqual(fmt_bytes(12_000), "12 KB")
        self.assertEqual(fmt_bytes(3_400_000), "3.4 MB")
        self.assertEqual(fmt_bytes(1_200_000_000), "1.2 GB")
        self.assertEqual(fmt_bytes(2_000_000_000_000), "2.0 TB")

    def test_bad_input_is_zero_bytes(self):
        self.assertEqual(fmt_bytes(None), "0 B")
        self.assertEqual(fmt_bytes("x"), "0 B")
        self.assertEqual(fmt_bytes(-5), "0 B")


class StorageOverviewTest(unittest.TestCase):
    def test_set_quota_shows_of_and_percent(self):
        self.assertEqual(
            storage_overview(ENVELOPE),
            "2.5 GB of 10.0 GB used (25%)",
        )

    def test_unlimited_shows_used_only(self):
        line = storage_overview({"usedBytes": 3_400_000, "quotaBytes": 0})
        self.assertEqual(line, "3.4 MB used")

    def test_percent_computed_when_server_omits_it(self):
        line = storage_overview(
            {"usedBytes": 5_000_000_000, "quotaBytes": 10_000_000_000}
        )
        self.assertEqual(line, "5.0 GB of 10.0 GB used (50%)")

    def test_empty_or_missing_used_yields_blank_line(self):
        self.assertEqual(storage_overview(None), "")
        self.assertEqual(storage_overview({}), "")
        self.assertEqual(storage_overview({"quotaBytes": 10}), "")


if __name__ == "__main__":
    unittest.main()
