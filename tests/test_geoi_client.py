import io
import json
import os
import sys
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import geoi_client  # noqa: E402
from geoi.geoi_client import (  # noqa: E402
    GeoiClient,
    GeoiError,
    epsg_from_spatial_reference,
    normalize_base,
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
    """Records the last urllib Request and returns queued responses."""

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


def client(responses):
    return GeoiClient(base_url="https://geoi.de", opener=FakeOpener(responses))


class NormalizeBaseTest(unittest.TestCase):
    def test_defaults_and_scheme(self):
        self.assertEqual(normalize_base(""), "https://www.geoi.de")
        self.assertEqual(normalize_base("geoi.de"), "https://geoi.de")
        self.assertEqual(normalize_base("https://x.test/"), "https://x.test")


class RedirectTest(unittest.TestCase):
    """The redirect handler must keep POST a POST (the geoi.de -> www.geoi.de
    301 was turning publish/save into a GET that returned the listing)."""

    def _redo(self, newurl, headers):
        h = geoi_client._ApiRedirect()
        req = urllib.request.Request(
            "https://geoi.de/platform/hub/services",
            data=b"BODY", headers=headers, method="POST",
        )
        new = h.redirect_request(req, None, 301, "Moved", {}, newurl)
        return new, {k.lower(): v for k, v in new.header_items()}

    def test_preserves_post_body_and_auth_same_site(self):
        new, hdrs = self._redo(
            "https://www.geoi.de/platform/hub/services",
            {"Authorization": "Bearer T", "Content-Type": "application/json"},
        )
        self.assertEqual(new.get_method(), "POST")
        self.assertEqual(new.data, b"BODY")
        self.assertEqual(hdrs.get("authorization"), "Bearer T")

    def test_drops_auth_cross_site_but_keeps_method(self):
        new, hdrs = self._redo(
            "https://evil.example.com/platform/hub/services",
            {"Authorization": "Bearer T"},
        )
        self.assertEqual(new.get_method(), "POST")
        self.assertNotIn("authorization", hdrs)

    def test_same_site_helper(self):
        self.assertTrue(geoi_client._same_site("geoi.de", "www.geoi.de"))
        self.assertTrue(geoi_client._same_site("a.geoi.de", "b.geoi.de"))
        self.assertFalse(geoi_client._same_site("geoi.de", "evil.com"))


class RequestTest(unittest.TestCase):
    def test_auth_config_no_bearer(self):
        c = client([(json.dumps({"success": True, "googleClientId": "abc"}), 200)])
        cfg = c.auth_config()
        self.assertEqual(cfg["googleClientId"], "abc")
        req = c._opener.requests[0]
        self.assertNotIn("Authorization", dict(req.header_items()))
        self.assertTrue(req.full_url.endswith("/platform/auth/config"))

    def test_auth_providers_parses_endpoint(self):
        # The SAME /auth/providers source the website reads (#585). The plugin
        # never hardcodes the list; it just relays what the server enabled.
        c = client([(json.dumps({
            "success": True,
            "providers": [
                {"id": "google", "label": "Google", "icon": "google", "clientId": "g"},
                {"id": "microsoft", "label": "Microsoft", "icon": "microsoft"},
            ],
        }), 200)])
        providers = c.auth_providers()
        self.assertEqual([p["id"] for p in providers], ["google", "microsoft"])
        req = c._opener.requests[0]
        # public endpoint: no bearer, hits /auth/providers
        self.assertNotIn("Authorization", dict(req.header_items()))
        self.assertTrue(req.full_url.endswith("/platform/auth/providers"))

    def test_auth_providers_parses_arcgis_entry(self):
        # ArcGIS (#685) surfaces through the SAME /auth/providers list with no
        # plugin change: the client relays whatever the server enabled, so an
        # `arcgis` entry is parsed and passed through untouched (the hosted
        # desktop-signin page renders the button + runs the server loopback).
        c = client([(json.dumps({
            "success": True,
            "providers": [
                {"id": "google", "label": "Google", "icon": "google", "clientId": "g"},
                {"id": "arcgis", "label": "ArcGIS", "icon": "arcgis"},
            ],
        }), 200)])
        providers = c.auth_providers()
        self.assertEqual([p["id"] for p in providers], ["google", "arcgis"])
        arcgis = providers[1]
        self.assertEqual(arcgis["label"], "ArcGIS")
        self.assertEqual(arcgis["icon"], "arcgis")
        req = c._opener.requests[0]
        self.assertNotIn("Authorization", dict(req.header_items()))
        self.assertTrue(req.full_url.endswith("/platform/auth/providers"))

    def test_build_signin_url_is_provider_agnostic(self):
        # The loopback URL carries only port + state — no per-provider data —
        # so adding ArcGIS server-side needs no change to build_signin_url.
        from geoi import auth
        url = auth.build_signin_url("https://www.geoi.de", 50123, "nonce")
        self.assertTrue(url.startswith("https://www.geoi.de/desktop-signin.html?"))
        self.assertIn("port=50123", url)
        self.assertIn("state=nonce", url)
        self.assertNotIn("provider", url)

    def test_auth_providers_empty_disables_sign_in(self):
        # No enabled provider -> [] -> the plugin offers no "Sign in".
        c = client([(json.dumps({"success": True, "providers": []}), 200)])
        self.assertEqual(c.auth_providers(), [])

    def test_auth_providers_missing_or_malformed_is_empty(self):
        for body in ({"success": True}, {"providers": "nope"}):
            c = client([(json.dumps(body), 200)])
            self.assertEqual(c.auth_providers(), [])

    def test_exchange_google_stores_token(self):
        c = client([(json.dumps({"success": True, "token": "T", "user": {"id": 1}}), 200)])
        res = c.exchange_google("idtok")
        self.assertEqual(res["token"], "T")
        self.assertEqual(c.token, "T")
        body = json.loads(c._opener.requests[0].data.decode())
        self.assertEqual(body["credential"], "idtok")

    def test_bearer_attached_when_token_set(self):
        c = client([(json.dumps({"success": True, "services": []}), 200)])
        c.set_token("SESS")
        c.list_services()
        headers = {k.lower(): v for k, v in c._opener.requests[0].header_items()}
        self.assertEqual(headers["authorization"], "Bearer SESS")

    def test_list_endpoints_unwrap(self):
        c = client([(json.dumps({"success": True, "services": [{"name": "a"}]}), 200)])
        self.assertEqual(c.list_services()[0]["name"], "a")

    def test_error_envelope_raises(self):
        c = client([(json.dumps({"error": {"code": 499, "message": "Token Required"}}), 200)])
        with self.assertRaises(GeoiError) as ctx:
            c.list_projects()
        self.assertEqual(ctx.exception.code, 499)
        self.assertIn("Token Required", str(ctx.exception))

    def test_http_error_raises(self):
        c = client([(json.dumps({"error": {"message": "nope"}}), 401)])
        with self.assertRaises(GeoiError) as ctx:
            c.me()
        self.assertEqual(ctx.exception.status, 401)

    def test_feature_server_url(self):
        c = client([])
        self.assertEqual(
            c.feature_server_url("My Svc"),
            "https://geoi.de/platform/rest/services/My%20Svc/FeatureServer",
        )
        self.assertTrue(c.feature_server_url("s", 0).endswith("/FeatureServer/0"))

    def test_publish_is_multipart(self):
        c = client([(json.dumps({"success": True, "service": {"name": "new"}}), 200)])
        svc = c.publish_service("p.geojson", b'{"type":"FeatureCollection"}')
        self.assertEqual(svc["name"], "new")
        req = c._opener.requests[0]
        ctype = dict((k.lower(), v) for k, v in req.header_items())["content-type"]
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="p.geojson"', req.data)

    def test_service_feature_layers(self):
        c = client([(json.dumps({"layers": [{"id": 0, "name": "Points"}]}), 200)])
        layers = c.service_feature_layers("svc")
        self.assertEqual(layers[0]["name"], "Points")
        self.assertTrue(
            c._opener.requests[0].full_url.endswith(
                "/platform/rest/services/svc/FeatureServer?f=json"
            )
        )

    def test_save_project_payload(self):
        c = client([(json.dumps({"success": True, "project": {"id": 7}}), 200)])
        proj = c.save_project("My Project", {"type": "FeatureCollection"})
        self.assertEqual(proj["id"], 7)
        body = json.loads(c._opener.requests[0].data.decode())
        self.assertEqual(body["name"], "My Project")
        self.assertEqual(body["data"]["type"], "FeatureCollection")

    def test_publish_raises_when_server_creates_nothing(self):
        # 2xx but no `service` in the body must NOT be treated as success
        c = client([(json.dumps({"success": True}), 200)])
        with self.assertRaises(GeoiError):
            c.publish_service("p.geojson", b'{"type":"FeatureCollection"}')

    def test_save_raises_when_server_saves_nothing(self):
        c = client([(json.dumps({"success": True}), 200)])
        with self.assertRaises(GeoiError):
            c.save_project("n", {"type": "FeatureCollection"})

    def test_log_hook_records_requests(self):
        msgs = []
        c = GeoiClient(
            base_url="https://geoi.de",
            opener=FakeOpener([(json.dumps({"success": True, "services": []}), 200)]),
            log=msgs.append,
        )
        c.list_services()
        self.assertTrue(
            any("GET /hub/services" in m and "HTTP 200" in m for m in msgs), msgs
        )


class ManageTest(unittest.TestCase):
    def test_update_service_sends_only_given_fields(self):
        c = client([(json.dumps({"success": True, "service": {"name": "s"}}), 200)])
        c.set_token("T")
        c.update_service("s", visibility="public", editable=True)
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/hub/services/s"))
        body = json.loads(req.data.decode())
        self.assertEqual(body["visibility"], "public")
        self.assertEqual(body["editable"], "true")
        self.assertNotIn("folderId", body)  # untouched when not passed

    def test_update_service_folder_empty_clears(self):
        c = client([(json.dumps({"success": True, "service": {"name": "s"}}), 200)])
        c.set_token("T")
        c.set_service_folder("s", "")
        body = json.loads(c._opener.requests[0].data.decode())
        self.assertEqual(body["folderId"], "")

    def test_update_service_folder_id_moves(self):
        c = client([(json.dumps({"success": True, "service": {"name": "s"}}), 200)])
        c.set_service_folder("s", "abc123")
        body = json.loads(c._opener.requests[0].data.decode())
        self.assertEqual(body["folderId"], "abc123")

    def test_share_service_with_group(self):
        c = client([(json.dumps({"success": True, "service": {"name": "s"}}), 200)])
        c.share_service_with_group("s", 7)
        req = c._opener.requests[0]
        self.assertTrue(req.full_url.endswith("/hub/services/s/shares"))
        self.assertEqual(json.loads(req.data.decode())["groupId"], 7)

    def test_delete_service(self):
        c = client([(json.dumps({"success": True, "deleted": "s"}), 200)])
        self.assertTrue(c.delete_service("s"))
        self.assertEqual(c._opener.requests[0].get_method(), "DELETE")

    def test_delete_project(self):
        c = client([(json.dumps({"success": True, "deleted": 4}), 200)])
        self.assertTrue(c.delete_project(4))
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "DELETE")
        self.assertTrue(req.full_url.endswith("/hub/projects/4"))

    def test_folder_crud(self):
        c = client([
            (json.dumps({"success": True, "folder": {"id": "f1"}}), 200),
            (json.dumps({"success": True, "folder": {"id": "f1"}}), 200),
            (json.dumps({"success": True, "folder": {"id": "f1"}}), 200),
            (json.dumps({"success": True, "deleted": "f1"}), 200),
        ])
        c.create_folder("Maps", parent_id="p0")
        c.rename_folder("f1", "Atlas")
        c.move_folder("f1", "p2")
        c.delete_folder("f1", force=True)
        r = c._opener.requests
        self.assertEqual(json.loads(r[0].data.decode()),
                         {"title": "Maps", "parentId": "p0"})
        self.assertEqual(json.loads(r[1].data.decode()), {"title": "Atlas"})
        self.assertEqual(json.loads(r[2].data.decode()), {"parentId": "p2"})
        self.assertEqual(r[3].get_method(), "DELETE")
        self.assertTrue(r[3].full_url.endswith("/hub/folders/f1?force=1"))

    def test_share_and_unshare_project_and_service_groups(self):
        c = client([
            (json.dumps({"success": True, "project": {"id": 5}}), 200),
            (json.dumps({"success": True, "project": {"id": 5}}), 200),
            (json.dumps({"success": True, "service": {"name": "s"}}), 200),
        ])
        c.share_project_with_group(5, 9)
        c.unshare_project_group(5, 9)
        c.unshare_service_group("s", 9)
        r = c._opener.requests
        self.assertTrue(r[0].full_url.endswith("/hub/projects/5/shares"))
        self.assertEqual(json.loads(r[0].data.decode())["groupId"], 9)
        self.assertEqual(r[1].get_method(), "DELETE")
        self.assertTrue(r[1].full_url.endswith("/hub/projects/5/shares/9"))
        self.assertEqual(r[2].get_method(), "DELETE")
        self.assertTrue(r[2].full_url.endswith("/hub/services/s/shares/9"))

    def test_list_groups(self):
        c = client([(json.dumps({"success": True,
                                 "groups": [{"id": 1, "name": "Team"}]}), 200)])
        self.assertEqual(c.list_groups()[0]["name"], "Team")

    def test_publish_with_options_applies_visibility_and_shares(self):
        c = client([
            (json.dumps({"success": True, "service": {"name": "new"}}), 200),
            (json.dumps({"success": True,
                         "service": {"name": "new", "visibility": "groups"}}), 200),
            (json.dumps({"success": True, "service": {"name": "new"}}), 200),
        ])
        svc = c.publish_service_with_options(
            "p.geojson", b'{"type":"FeatureCollection"}',
            visibility="groups", editable=True, group_ids=[3],
        )
        self.assertEqual(svc["name"], "new")
        # 1) multipart upload, 2) POST options, 3) POST share
        self.assertEqual(len(c._opener.requests), 3)
        opts = json.loads(c._opener.requests[1].data.decode())
        self.assertEqual(opts["visibility"], "groups")
        self.assertEqual(opts["editable"], "true")
        self.assertEqual(json.loads(c._opener.requests[2].data.decode())["groupId"], 3)

    def test_publish_with_options_private_skips_update(self):
        c = client([(json.dumps({"success": True, "service": {"name": "new"}}), 200)])
        c.publish_service_with_options(
            "p.geojson", b'{"type":"FeatureCollection"}', visibility="private",
        )
        self.assertEqual(len(c._opener.requests), 1)  # upload only


class SpatialReferenceTest(unittest.TestCase):
    """A Feature Service must be added in its OWN CRS — the platform serves
    Web Mercator (3857); declaring 4326 silently corrupts edits on commit."""

    def test_web_mercator_aliases_normalise_to_3857(self):
        self.assertEqual(
            epsg_from_spatial_reference({"wkid": 102100, "latestWkid": 3857}),
            "EPSG:3857")
        self.assertEqual(epsg_from_spatial_reference(102100), "EPSG:3857")
        self.assertEqual(epsg_from_spatial_reference(3857), "EPSG:3857")

    def test_plain_wkid(self):
        self.assertEqual(epsg_from_spatial_reference({"wkid": 4326}), "EPSG:4326")
        self.assertEqual(epsg_from_spatial_reference(25832), "EPSG:25832")

    def test_latest_wkid_wins(self):
        self.assertEqual(
            epsg_from_spatial_reference({"wkid": 102100, "latestWkid": 3857}),
            "EPSG:3857")

    def test_missing_or_bad_falls_back_to_web_mercator(self):
        self.assertEqual(epsg_from_spatial_reference(None), "EPSG:3857")
        self.assertEqual(epsg_from_spatial_reference({}), "EPSG:3857")
        self.assertEqual(epsg_from_spatial_reference("nope"), "EPSG:3857")

    def test_feature_server_info_returns_root(self):
        c = client([(json.dumps({
            "layers": [{"id": 0, "name": "Pts"}],
            "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        }), 200)])
        info = c.feature_server_info("svc")
        self.assertEqual(epsg_from_spatial_reference(info["spatialReference"]),
                         "EPSG:3857")
        self.assertEqual(info["layers"][0]["name"], "Pts")


class TileServicesTest(unittest.TestCase):
    """P4 — list published tile services and build copy-ready per-format URLs."""

    def test_tile_services_unwraps_list(self):
        c = client([(json.dumps({"ok": True, "services": [
            {"id": 5, "title": "Ortho", "slug": "ortho", "visibility": "public",
             "minZoom": 0, "maxZoom": 19, "pmtilesUrl": "/p/5.pmtiles"},
        ]}), 200)])
        c.set_token("T")
        svcs = c.tile_services()
        self.assertEqual(svcs[0]["title"], "Ortho")
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.endswith("/platform/raster/services"))

    def test_tile_services_missing_or_malformed_is_empty(self):
        for body in ({"ok": True}, {"services": "nope"}):
            c = client([(json.dumps(body), 200)])
            self.assertEqual(c.tile_services(), [])

    def test_tile_service_unwraps_detail(self):
        c = client([(json.dumps({"ok": True, "service": {
            "id": 5, "title": "Ortho", "visibility": "public",
            "urls": {"pmtiles": "/p/5.pmtiles",
                     "xyz": "/x/5/{z}/{x}/{y}.webp",
                     "wmts": "/w/5/WMTSCapabilities.xml"},
        }}), 200)])
        c.set_token("T")
        detail = c.tile_service(5)
        self.assertEqual(detail["urls"]["xyz"], "/x/5/{z}/{x}/{y}.webp")
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.endswith("/platform/raster/services/5"))

    def test_tile_service_empty_when_absent(self):
        c = client([(json.dumps({"ok": True}), 200)])
        self.assertEqual(c.tile_service(9), {})

    def test_tile_url_absolutizes_relative(self):
        c = client([])
        self.assertEqual(
            c.tile_url("/platform/raster/services/5/xyz/{z}/{x}/{y}.webp"),
            "https://geoi.de/platform/raster/services/5/xyz/{z}/{x}/{y}.webp",
        )

    def test_tile_url_absolute_unchanged(self):
        c = client([])
        absolute = "https://cdn.example.com/t/5/{z}/{x}/{y}.webp"
        self.assertEqual(c.tile_url(absolute), absolute)

    def test_tile_url_no_token_for_public(self):
        c = client([])
        url = c.tile_url("/x/5/{z}/{x}/{y}.webp",
                         share_token="SEKRET", visibility="public")
        self.assertNotIn("token=", url)

    def test_tile_url_appends_token_for_non_public_no_query(self):
        c = client([])
        url = c.tile_url("/x/5/{z}/{x}/{y}.webp",
                         share_token="SEKRET", visibility="private")
        self.assertTrue(url.endswith("?token=SEKRET"))

    def test_tile_url_appends_token_with_ampersand_when_query_present(self):
        c = client([])
        url = c.tile_url("https://cdn.test/w/5/cap.xml?service=WMTS",
                         share_token="SEKRET", visibility="private")
        self.assertTrue(url.endswith("&token=SEKRET"))
        self.assertIn("?service=WMTS", url)

    def test_tile_url_token_percent_encoded(self):
        c = client([])
        url = c.tile_url("/x/5/{z}/{x}/{y}.webp",
                         share_token="a b/c", visibility="private")
        self.assertTrue(url.endswith("?token=a%20b%2Fc"))

    def test_tile_url_empty_input_is_empty(self):
        c = client([])
        self.assertEqual(c.tile_url(""), "")
        self.assertEqual(c.tile_url(None), "")

    def test_tile_format_urls_builds_all_three(self):
        c = client([])
        detail = {
            "visibility": "private",
            "shareToken": "TKN",
            "urls": {
                "pmtiles": "/p/5.pmtiles",
                "xyz": "/x/5/{z}/{x}/{y}.webp",
                "wmts": "https://cdn.test/w/5/WMTSCapabilities.xml",
            },
        }
        urls = c.tile_format_urls(detail)
        self.assertEqual(urls["pmtiles"],
                         "https://geoi.de/p/5.pmtiles?token=TKN")
        self.assertEqual(urls["xyz"],
                         "https://geoi.de/x/5/{z}/{x}/{y}.webp?token=TKN")
        self.assertEqual(
            urls["wmts"],
            "https://cdn.test/w/5/WMTSCapabilities.xml?token=TKN")

    def test_tile_format_urls_public_has_no_token(self):
        c = client([])
        detail = {"visibility": "public",
                  "urls": {"xyz": "/x/5/{z}/{x}/{y}.webp"}}
        urls = c.tile_format_urls(detail)
        self.assertEqual(urls["xyz"],
                         "https://geoi.de/x/5/{z}/{x}/{y}.webp")
        self.assertNotIn("wmts", urls)  # missing formats omitted

    def test_tile_services_feature_off_maps_via_friendly_error(self):
        c = client([(json.dumps({"error": "feature_off"}), 200)])
        with self.assertRaises(GeoiError) as ctx:
            c.tile_services()
        self.assertEqual(ctx.exception.code, "feature_off")
        self.assertIn("administrator", geoi_client.friendly_error(ctx.exception))


class TileServiceManageTest(unittest.TestCase):
    """Full management of a published tile service: rename, visibility, move,
    delete — each POSTs/DELETEs the right /raster/services/<id> route."""

    def _body(self, req):
        return json.loads(req.data.decode("utf-8"))

    def test_rename_posts_title_and_unwraps_service(self):
        c = client([(json.dumps({"ok": True, "service": {
            "id": 5, "title": "Renamed"}}), 200)])
        c.set_token("T")
        svc = c.rename_tile_service(5, "Renamed")
        self.assertEqual(svc["title"], "Renamed")
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/platform/raster/services/5"))
        self.assertEqual(self._body(req), {"title": "Renamed"})

    def test_set_visibility_posts_visibility(self):
        c = client([(json.dumps({"ok": True, "service": {
            "id": 5, "visibility": "public"}}), 200)])
        c.set_token("T")
        svc = c.set_tile_service_visibility(5, "public")
        self.assertEqual(svc["visibility"], "public")
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/platform/raster/services/5"))
        self.assertEqual(self._body(req), {"visibility": "public"})

    def test_move_posts_folder_id(self):
        c = client([(json.dumps({"ok": True, "service": {
            "id": 5, "folderId": "f9"}}), 200)])
        c.set_token("T")
        svc = c.move_tile_service(5, "f9")
        self.assertEqual(svc["folderId"], "f9")
        req = c._opener.requests[0]
        self.assertEqual(self._body(req), {"folderId": "f9"})

    def test_move_to_root_posts_empty_folder_id(self):
        c = client([(json.dumps({"ok": True, "service": {"id": 5}}), 200)])
        c.set_token("T")
        c.move_tile_service(5, None)
        self.assertEqual(self._body(c._opener.requests[0]), {"folderId": ""})

    def test_delete_issues_delete_and_returns_true(self):
        c = client([(json.dumps({"ok": True}), 200)])
        c.set_token("T")
        self.assertTrue(c.delete_tile_service(5))
        req = c._opener.requests[0]
        self.assertEqual(req.get_method(), "DELETE")
        self.assertTrue(req.full_url.endswith("/platform/raster/services/5"))

    def test_failure_raises_geoi_error_with_friendly_text(self):
        c = client([(json.dumps({"error": "quota_exceeded"}), 200)])
        c.set_token("T")
        with self.assertRaises(GeoiError) as ctx:
            c.rename_tile_service(5, "x")
        self.assertEqual(ctx.exception.code, "quota_exceeded")
        self.assertIn("quota", geoi_client.friendly_error(ctx.exception))


class MultipartTest(unittest.TestCase):
    def test_encode_multipart_roundtrip_markers(self):
        body, ctype = geoi_client.encode_multipart(
            {"file": ("a.json", b"DATA", "application/json")}
        )
        self.assertIn("boundary=", ctype)
        self.assertIn(b"DATA", body)
        self.assertIn(b'filename="a.json"', body)


if __name__ == "__main__":
    unittest.main()
