"""
geoi platform REST client.

Pure Python (standard library only) — no QGIS imports and no third-party
dependencies — so it is unit-testable off a QGIS install and trivial to
ship. It speaks the same endpoints the geoi web app uses, under
``<base>/platform/``:

  GET  /auth/config      sign-in availability + Google client ids
  GET  /auth/providers   the enabled sign-in providers (the website's source)
  POST /auth/google      exchange a Google ID token -> 30-day session token
  GET  /auth/me          current user
  POST /auth/signout     revoke the session
  GET  /hub/services      | /hub/projects | /hub/folders   (browse content)
  GET  /hub/services/<n>  service detail
  POST /hub/services      multipart publish a project as a Feature Service
  POST /hub/projects      save a project ("geoi package")

and builds the ``…/rest/services/<name>/FeatureServer`` URLs that QGIS's
ArcGIS Feature Service provider consumes natively.
"""

import json
import mimetypes
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_BASE_URL = "https://www.geoi.de"
PLATFORM_PREFIX = "/platform"
USER_AGENT = "geoi-qgis-plugin"

# Sentinel: "leave this field untouched" (distinct from None, which means
# "clear to root"). The hub reads an absent `folderId` as no change and an
# empty string as a move to the user's root.
_UNSET = object()


def _same_site(host_a, host_b):
    """True if two hosts share the same registrable domain (last two labels)."""
    if not host_a or not host_b:
        return False
    if host_a == host_b:
        return True
    a, b = host_a.split("."), host_b.split(".")
    return len(a) >= 2 and a[-2:] == b[-2:]


class _ApiRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects WITHOUT downgrading POST->GET.

    Default urllib turns a redirected POST/PUT/DELETE into a GET and drops the
    body — and strips Authorization on a host change. The geoi platform issues
    a 301 from geoi.de to www.geoi.de, so a POST /hub/* silently became a GET
    and returned the *listing* instead of creating. Re-issue with the SAME
    method + body, keeping the bearer only on same-site redirects.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        same = _same_site(
            urllib.parse.urlsplit(req.full_url).hostname,
            urllib.parse.urlsplit(newurl).hostname,
        )
        new_headers = {}
        for key, value in req.header_items():
            kl = key.lower()
            if kl == "content-length":
                continue  # urllib recomputes it for the re-sent body
            if kl == "authorization" and not same:
                continue  # never forward the bearer to another site
            new_headers[key] = value
        return urllib.request.Request(
            newurl,
            data=req.data,
            headers=new_headers,
            method=req.get_method(),
            origin_req_host=req.origin_req_host,
            unverifiable=True,
        )


class GeoiError(Exception):
    """A platform or transport error, with the server's message when known."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# Friendly, actionable text for the raster-publish error CODES the platform
# emits (api/raster.php). Clients key off the stable `error` CODE, never the
# server's English message — so a server wording change never breaks this map.
RASTER_ERROR_MESSAGES = {
    "feature_off": (
        "Raster tile publishing isn't enabled on this geoi server yet — ask "
        "your administrator to turn it on (Account → Administration → Tile "
        "Services)."
    ),
    "quota_exceeded": (
        "Your raster storage quota is full. Delete an old tile set and try "
        "again."
    ),
    "unauthorized": "Please sign in to geoi first, then retry.",
    "rate_limited": "Too many uploads just now — wait a minute and try again.",
    "bad_request": "The server rejected the upload request.",
}


def friendly_error(exc):
    """Map a raster-publish error to a clear, actionable sentence.

    Prefers the friendly text for a known server error CODE, then the server's
    own message, then the bare string — so an unmapped code is behaviourally
    identical to the old "show the message" path.
    """
    return (
        RASTER_ERROR_MESSAGES.get(getattr(exc, "code", None))
        or getattr(exc, "message", None)
        or str(exc)
    )


def normalize_base(base_url):
    """Trim and default a base URL; assume https when no scheme is given."""
    base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        return DEFAULT_BASE_URL
    if "://" not in base:
        base = "https://" + base
    return base


class GeoiClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, token=None, timeout=30, opener=None, log=None):
        self.base_url = normalize_base(base_url)
        self.token = token or None
        self.timeout = timeout
        # `opener` is injectable for tests; production uses a default opener
        # with system trust + TLS verification.
        self._opener = opener or urllib.request.build_opener(_ApiRedirect)
        # optional diagnostic sink: log(str) is called for every request.
        self._log = log

    # ------------------------------------------------------------------ auth
    def set_token(self, token):
        self.token = token or None

    def auth_config(self):
        return self._request("GET", "/auth/config", auth=False)

    def auth_providers(self, timeout=None):
        """The enabled sign-in providers (#585) — the SAME source the website
        reads. Returns the list of ``{id, label, icon, clientId?}`` entries;
        ``[]`` means sign-in is not configured. NO hardcoded provider list:
        the provider choice is rendered by the hosted desktop-signin page.

        ``timeout`` overrides the client default so a caller (the sign-in task)
        can run a SHORT, non-blocking availability pre-check and not freeze on a
        slow ``/auth/providers`` round-trip.
        """
        res = self._request("GET", "/auth/providers", auth=False, timeout=timeout)
        providers = res.get("providers") if isinstance(res, dict) else None
        return providers if isinstance(providers, list) else []

    def exchange_google(self, id_token):
        """POST a Google ID token, store and return the session token + user."""
        res = self._request(
            "POST", "/auth/google", json_body={"credential": id_token}, auth=False
        )
        token = res.get("token")
        if not token:
            raise GeoiError("Sign-in did not return a session token.")
        self.token = token
        return res

    def me(self):
        return self._request("GET", "/auth/me")

    def signout(self):
        try:
            self._request("POST", "/auth/signout")
        finally:
            self.token = None

    # ------------------------------------------------------------- catalogue
    def list_services(self):
        return self._request("GET", "/hub/services").get("services", [])

    def list_projects(self):
        return self._request("GET", "/hub/projects").get("projects", [])

    def list_folders(self):
        return self._request("GET", "/hub/folders").get("folders", [])

    def list_groups(self, timeout=None):
        return self._request("GET", "/hub/groups", timeout=timeout).get("groups", [])

    def storage(self, timeout=None):
        """The signed-in user's storage usage (#677).

        ``GET /hub/storage`` -> ``{ok, usedBytes, quotaBytes, percent,
        breakdown:{feature, tile, project, app}}``. ``quotaBytes == 0`` means
        the account is UNLIMITED (show the used figure, no percent/bar). The
        bearer rides via ``_request``; the caller catches a ``GeoiError`` and
        hides the line, so a missing/slow endpoint never blocks sign-in.
        """
        res = self._request("GET", "/hub/storage", timeout=timeout)
        return res if isinstance(res, dict) else {}

    def get_service(self, name):
        path = "/hub/services/" + urllib.parse.quote(name, safe="")
        return self._request("GET", path).get("service", {})

    # ------------------------------------------------------- manage services
    def _svc_path(self, name, *suffix):
        path = "/hub/services/" + urllib.parse.quote(name, safe="")
        for part in suffix:
            path += "/" + str(part)
        return path

    def update_service(self, name, *, visibility=None, editable=None,
                       title=None, folder_id=_UNSET):
        """Change a service's visibility / editability / title / folder.

        ``folder_id`` is a sentinel: leave it out to keep the current folder,
        pass ``""``/``None`` to move to the root, or a folder id to move in.
        """
        payload = {}
        if visibility is not None:
            payload["visibility"] = visibility
        if editable is not None:
            payload["editable"] = "true" if editable else "false"
        if title is not None:
            payload["title"] = title
        if folder_id is not _UNSET:
            payload["folderId"] = folder_id or ""
        res = self._request("POST", self._svc_path(name), json_body=payload)
        return res.get("service", {}) if isinstance(res, dict) else {}

    def set_service_folder(self, name, folder_id):
        """Move a service into a folder (id) or to the root (``""``/None)."""
        return self.update_service(name, folder_id=folder_id)

    def share_service_with_group(self, name, group_id):
        res = self._request(
            "POST", self._svc_path(name, "shares"), json_body={"groupId": group_id}
        )
        return res.get("service", {}) if isinstance(res, dict) else {}

    def delete_service(self, name):
        self._request("DELETE", self._svc_path(name))
        return True

    # ------------------------------------------------------- manage projects
    def update_project(self, project_id, *, name=None, visibility=None,
                       folder_id=_UNSET):
        payload = {}
        if name is not None:
            payload["name"] = name
        if visibility is not None:
            payload["visibility"] = visibility
        if folder_id is not _UNSET:
            payload["folderId"] = folder_id or ""
        res = self._request(
            "POST", "/hub/projects/{}".format(int(project_id)), json_body=payload
        )
        return res.get("project", {}) if isinstance(res, dict) else {}

    def set_project_folder(self, project_id, folder_id):
        return self.update_project(project_id, folder_id=folder_id)

    def share_project_with_group(self, project_id, group_id):
        res = self._request(
            "POST", "/hub/projects/{}/shares".format(int(project_id)),
            json_body={"groupId": group_id},
        )
        return res.get("project", {}) if isinstance(res, dict) else {}

    def unshare_project_group(self, project_id, group_id):
        self._request(
            "DELETE", "/hub/projects/{}/shares/{}".format(int(project_id), int(group_id))
        )
        return True

    def unshare_service_group(self, name, group_id):
        self._request("DELETE", self._svc_path(name, "shares", int(group_id)))
        return True

    def delete_project(self, project_id):
        self._request("DELETE", "/hub/projects/{}".format(int(project_id)))
        return True

    # -------------------------------------------------------- manage folders
    def create_folder(self, title, parent_id=None):
        payload = {"title": title}
        if parent_id:
            payload["parentId"] = parent_id
        return self._request("POST", "/hub/folders", json_body=payload).get("folder", {})

    def rename_folder(self, folder_id, title):
        path = "/hub/folders/" + urllib.parse.quote(str(folder_id), safe="")
        return self._request("POST", path, json_body={"title": title}).get("folder", {})

    def move_folder(self, folder_id, parent_id):
        """Re-parent a folder under ``parent_id`` (or to the root with None/"")."""
        path = "/hub/folders/" + urllib.parse.quote(str(folder_id), safe="")
        return self._request(
            "POST", path, json_body={"parentId": parent_id or ""}
        ).get("folder", {})

    def delete_folder(self, folder_id, force=False):
        path = "/hub/folders/" + urllib.parse.quote(str(folder_id), safe="")
        if force:
            path += "?force=1"
        self._request("DELETE", path)
        return True

    # --------------------------------------------------------------- consume
    def service_feature_layers(self, service_name):
        """List a Feature Service's layers as [{id, name}, ...]."""
        return self.feature_server_info(service_name).get("layers", [])

    def feature_server_info(self, service_name):
        """The FeatureServer root metadata (``layers`` + ``spatialReference``)."""
        path = "/rest/services/{}/FeatureServer?f=json".format(
            urllib.parse.quote(service_name, safe="")
        )
        data = self._request("GET", path)
        return data if isinstance(data, dict) else {}

    def feature_server_url(self, service_name, layer_id=None):
        url = "{base}{p}/rest/services/{name}/FeatureServer".format(
            base=self.base_url,
            p=PLATFORM_PREFIX,
            name=urllib.parse.quote(service_name, safe=""),
        )
        if layer_id is not None:
            url += "/{}".format(int(layer_id))
        return url

    # --------------------------------------------------------- publish / save
    def publish_service(self, filename, data, content_type="application/geo+json"):
        """Publish a project file as a (private) Feature Service."""
        body, ctype = encode_multipart({"file": (filename, data, content_type)})
        res = self._request("POST", "/hub/services", raw_body=body, content_type=ctype)
        svc = res.get("service") if isinstance(res, dict) else None
        if not isinstance(svc, dict) or not svc.get("name"):
            # The request returned 2xx but the server did not confirm a new
            # service — surface it instead of silently "succeeding".
            raise GeoiError(
                "The server accepted the request but did not create a service. "
                "Response: " + _short(res)
            )
        return svc

    def publish_service_with_options(self, filename, data, *,
                                     content_type="application/geo+json",
                                     visibility=None, editable=None,
                                     group_ids=None, folder_id=_UNSET):
        """Publish a project file, then apply visibility/editable/sharing.

        The upload endpoint always creates a *private* service; this wraps it
        so the user's choices in the publish dialog actually take effect.
        Returns the final service entry.
        """
        svc = self.publish_service(filename, data, content_type)
        name = svc.get("name")
        needs_update = (
            (visibility and visibility != "private")
            or editable
            or folder_id is not _UNSET
        )
        if name and needs_update:
            updated = self.update_service(
                name,
                visibility=visibility if visibility != "private" else None,
                editable=editable if editable else None,
                folder_id=folder_id,
            )
            if updated:
                svc = updated
        if name and visibility == "groups":
            for gid in group_ids or []:
                try:
                    svc = self.share_service_with_group(name, gid) or svc
                except GeoiError:
                    pass
        return svc

    # ------------------------------------------------------- raster tiles
    def raster_presign(self, name, byte_count):
        """Ask the platform for a presigned PUT URL for a ``.pmtiles`` upload.

        ``POST /raster/presign {name, bytes}`` -> ``{uploadUrl, objectKey,
        publicUrl?}``. ``uploadUrl`` is a short-lived, self-authenticating
        store URL (Backblaze B2 S3) the client PUTs the file to directly —
        WITHOUT our bearer (see ``put_url``).
        """
        res = self._request(
            "POST", "/raster/presign",
            json_body={"name": name, "bytes": int(byte_count)},
        )
        if not isinstance(res, dict) or not res.get("uploadUrl"):
            raise GeoiError(
                "The server did not return an upload URL for the raster tiles. "
                "Response: " + _short(res)
            )
        return res

    def raster_confirm(self, object_key, byte_count, bounds=None):
        """Finalise a raster upload after the store PUT succeeds.

        ``POST /raster/confirm {objectKey, bytes, bounds?}`` — the server
        verifies the object and records the catalogue entry, returning the
        public read URL.
        """
        payload = {"objectKey": object_key, "bytes": int(byte_count)}
        if bounds is not None:
            payload["bounds"] = bounds
        res = self._request("POST", "/raster/confirm", json_body=payload)
        return res if isinstance(res, dict) else {}

    def raster_list(self):
        """List the signed-in user's published raster tile sets."""
        res = self._request("GET", "/raster/mine")
        # The server emits the list under "layers" (api/raster.php).
        layers = res.get("layers") if isinstance(res, dict) else None
        return layers if isinstance(layers, list) else []

    # ------------------------------------------------------- tile services
    def tile_services(self, timeout=None):
        """List the user's published raster TILE SERVICES (P4).

        ``GET /raster/services`` -> ``{services: [{id, title, slug,
        visibility, backend, bytes, minZoom, maxZoom, created, updated,
        pmtilesUrl}]}``. The summary carries enough (id, title, visibility)
        for the tree; the per-format URLs come from ``tile_service``.

        ``timeout`` overrides the client default for this OPTIONAL call so a
        slow/missing endpoint can't stall the post-sign-in content load.
        """
        res = self._request("GET", "/raster/services", timeout=timeout)
        services = res.get("services") if isinstance(res, dict) else None
        return services if isinstance(services, list) else []

    def tile_service(self, service_id):
        """Detail for one tile service, incl. the per-format URLs (P4).

        ``GET /raster/services/<id>`` -> ``{service: {…, urls: {pmtiles,
        xyz, wmts}, shareToken?}}``. ``urls.xyz`` is a ``{z}/{x}/{y}.webp``
        template, ``urls.wmts`` the WMTSCapabilities.xml URL, ``urls.pmtiles``
        the archive URL — each possibly root-relative (absolutize with
        ``tile_url``). ``shareToken`` is present only for a non-public
        service (append it with ``tile_url`` so the URL works without
        sign-in).
        """
        path = "/raster/services/" + urllib.parse.quote(str(service_id), safe="")
        res = self._request("GET", path)
        svc = res.get("service") if isinstance(res, dict) else None
        return svc if isinstance(svc, dict) else {}

    def _tile_path(self, service_id):
        return "/raster/services/" + urllib.parse.quote(str(service_id), safe="")

    def rename_tile_service(self, service_id, title):
        """Rename a published tile service (``POST /raster/services/<id>
        {title}`` -> ``{ok, service}``). Returns the updated service dict."""
        res = self._request(
            "POST", self._tile_path(service_id), json_body={"title": title}
        )
        return res.get("service", {}) if isinstance(res, dict) else {}

    def set_tile_service_visibility(self, service_id, visibility):
        """Change a tile service's visibility (``private``|``groups``|``public``)
        — ``POST /raster/services/<id> {visibility}`` -> ``{ok, service}``."""
        res = self._request(
            "POST", self._tile_path(service_id),
            json_body={"visibility": visibility},
        )
        return res.get("service", {}) if isinstance(res, dict) else {}

    def move_tile_service(self, service_id, folder_id):
        """Move a tile service into a folder (a ``content_folders`` id) or to
        the root (``""``/``None``) — ``POST /raster/services/<id> {folderId}``
        -> ``{ok, service{folderId}}``."""
        res = self._request(
            "POST", self._tile_path(service_id),
            json_body={"folderId": folder_id or ""},
        )
        return res.get("service", {}) if isinstance(res, dict) else {}

    def delete_tile_service(self, service_id):
        """Delete a published tile service (``DELETE /raster/services/<id>``
        -> ``{ok}``). Returns ``True``."""
        self._request("DELETE", self._tile_path(service_id))
        return True

    def tile_url(self, raw_url, *, share_token=None, visibility=None):
        """Build a copy-ready absolute tile URL from a service ``urls.*`` value.

        Mirrors how the geoi web client absolutizes + tokenizes a tile URL:

        * a ROOT-RELATIVE url (``/platform/raster/…``) is made absolute
          against ``base_url``; an already-absolute ``http(s)://`` url is
          returned host-and-path unchanged;
        * for a NON-public service a ``share_token`` is appended as
          ``?token=`` (or ``&token=`` when the url already has a query) so
          the link works in QGIS without a geoi sign-in. A public service
          gets no token.

        Pure / stdlib-only so it is unit-testable off QGIS.
        """
        url = (raw_url or "").strip()
        if not url:
            return ""
        if "://" not in url:
            url = self.base_url + ("/" + url.lstrip("/"))
        if share_token and visibility != "public":
            sep = "&" if "?" in url else "?"
            url += sep + "token=" + urllib.parse.quote(str(share_token), safe="")
        return url

    def tile_format_urls(self, detail):
        """Absolute, tokenized {xyz, wmts, pmtiles} URLs from a tile detail.

        ``detail`` is the dict from ``tile_service``; reads ``urls`` +
        ``shareToken`` + ``visibility`` and runs each through ``tile_url``.
        Missing formats are omitted.
        """
        if not isinstance(detail, dict):
            return {}
        urls = detail.get("urls") if isinstance(detail.get("urls"), dict) else {}
        token = detail.get("shareToken")
        visibility = detail.get("visibility")
        out = {}
        for fmt in ("xyz", "wmts", "pmtiles"):
            built = self.tile_url(
                urls.get(fmt), share_token=token, visibility=visibility
            )
            if built:
                out[fmt] = built
        return out

    def _absolutize_upload_url(self, url):
        """Resolve a presign ``uploadUrl`` to an absolute, PUT-able URL.

        The platform's two storage backends hand back two shapes:

        * Backblaze B2 — an already-absolute ``https://…`` presigned URL,
          returned unchanged (it self-authenticates and lives off-site).
        * Server (local) — a ROOT-RELATIVE, token-authenticated
          ``/platform/raster/upload?t=<token>`` URL. ``urllib`` cannot PUT
          to that ("unknown url type"), so resolve it against ``base_url``.
          The server's relative URL ALREADY carries the ``/platform`` prefix
          and ``base_url`` is the site root WITHOUT it, so a plain
          concatenation is correct.

        Neither shape changes the no-Authorization rule: the B2 URL is
        self-signed; the local URL is HMAC-authenticated by ``?t=``.
        """
        url = (url or "").strip()
        if not url:
            return url
        if url.startswith("http://") or url.startswith("https://"):
            return url
        # Protocol-relative ("//host/…") is NOT a same-origin path — leave it
        # untouched (neither backend emits it; never prefix base_url onto it).
        if url.startswith("//"):
            return url
        if url.startswith("/"):
            return self.base_url + url
        return url

    def put_url(self, url, file_path, content_type="application/octet-stream"):
        """PUT a file to an upload URL (the presigned object-store URL or the
        token-authenticated server-local upload URL).

        This bypasses ``_request`` (which prefixes the platform base) AND the
        ``_ApiRedirect`` opener, and it attaches NO ``Authorization`` header:
        the B2 presigned URL self-authenticates and the local ``?t=`` URL is
        HMAC-authenticated, and sending the geoi bearer to the object store (a
        different host) would leak our token. A ROOT-RELATIVE local upload URL
        is absolutized against ``base_url`` first (a raw ``urllib`` PUT to a
        scheme-less URL raises "unknown url type"). A direct ``urllib`` PUT
        with a default opener.
        """
        url = self._absolutize_upload_url(url)
        with open(file_path, "rb") as handle:
            data = handle.read()
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
        }
        # Explicitly NO Authorization header — never send our token off-site.
        req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
        # A plain default opener: NOT self._opener (which carries _ApiRedirect's
        # method-preserving bearer re-attach), so even on a redirect our token
        # can't ride. `_put_opener` is injectable for tests only.
        opener = getattr(self, "_put_opener", None)
        do_open = opener.open if opener is not None else urllib.request.urlopen
        try:
            with do_open(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None) or getattr(resp, "code", 200) or 200
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = (exc.read() or b"").decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise GeoiError(
                "Uploading the tiles to storage failed (HTTP {}). {}".format(
                    exc.code, body
                ),
                status=exc.code,
            )
        except (urllib.error.URLError, ssl.SSLError) as exc:
            reason = getattr(exc, "reason", exc)
            raise GeoiError("Network error uploading tiles: {}".format(reason))
        # Log a REDACTED url — the query string carries the SigV4 signature
        # (a short-lived write credential), which must never reach the log.
        safe_url = url.split("?", 1)[0]
        self._note("PUT {} ({} bytes) -> HTTP {}".format(safe_url, len(data), status))
        if status >= 400:
            raise GeoiError(
                "Uploading the tiles to storage failed (HTTP {}).".format(status),
                status=status,
            )
        return True

    def save_project(self, name, data, visibility=None):
        """Save a project document ("geoi package") to the platform."""
        payload = {"name": name, "data": data}
        if visibility:
            payload["visibility"] = visibility
        res = self._request("POST", "/hub/projects", json_body=payload)
        proj = res.get("project") if isinstance(res, dict) else None
        if not isinstance(proj, dict) or proj.get("id") in (None, ""):
            raise GeoiError(
                "The server accepted the request but did not save the project. "
                "Response: " + _short(res)
            )
        return proj

    def send_feedback(self, *, category, body, title="", email="",
                      source="qgis", app_version="", sysinfo="",
                      attachment_path=None):
        """Submit a feedback / bug report to /platform/feedback.

        Sign-in is OPTIONAL — the bearer token is attached only when present.
        Text fields ride as plain form fields; an optional screenshot/PDF
        rides as a file part. Returns the parsed {ok, id} response.
        """
        fields = {
            "category": category or "general",
            "title": title or "",
            "body": body or "",
            "email": email or "",
            "source": source or "qgis",
            "appVersion": app_version or "",
            "sysinfo": sysinfo or "",
        }
        files = None
        if attachment_path:
            with open(attachment_path, "rb") as handle:
                data = handle.read()
            ctype = mimetypes.guess_type(attachment_path)[0] or "application/octet-stream"
            files = {"file": (os.path.basename(attachment_path), data, ctype)}
        raw, ctype = encode_multipart_form(fields, files)
        res = self._request("POST", "/feedback", raw_body=raw,
                            content_type=ctype, auth=True)
        if not (isinstance(res, dict) and res.get("ok")):
            raise GeoiError(
                "The server did not accept the feedback. Response: " + _short(res)
            )
        return res

    # ------------------------------------------------------------- transport
    def _request(
        self,
        method,
        path,
        *,
        json_body=None,
        raw_body=None,
        content_type=None,
        auth=True,
        timeout=None,
    ):
        url = self.base_url + PLATFORM_PREFIX + path
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            body = raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8")
            if content_type:
                headers["Content-Type"] = content_type
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        eff_timeout = self.timeout if timeout is None else timeout
        try:
            with self._opener.open(req, timeout=eff_timeout) as resp:
                payload = resp.read()
                status = getattr(resp, "status", None) or getattr(resp, "code", 200) or 200
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status = exc.code
        except (urllib.error.URLError, ssl.SSLError) as exc:
            reason = getattr(exc, "reason", exc)
            self._note("{} {} -> network error: {}".format(method, path, reason))
            raise GeoiError("Network error: {}".format(reason))
        nbytes = len(body) if body else 0
        self._note("{} {} ({} bytes sent) -> HTTP {}".format(method, path, nbytes, status))
        return _parse(payload, status)

    def _note(self, msg):
        if self._log:
            try:
                self._log(msg)
            except Exception:  # noqa: BLE001 - diagnostics must never break a call
                pass


# --------------------------------------------------------------------- helpers
# Web Mercator is advertised by ESRI services as wkid 102100 (with
# latestWkid 3857) and a handful of historical aliases; QGIS wants the
# EPSG:3857 authority id.
_WEB_MERCATOR_WKIDS = {3857, 102100, 102113, 900913}


def epsg_from_spatial_reference(spatial_reference, default="EPSG:3857"):
    """Map a Feature Service ``spatialReference`` to a QGIS ``EPSG:<n>`` string.

    The geoi platform serves geometry in Web Mercator (3857), advertised as
    ``{"wkid": 102100, "latestWkid": 3857}``. The layer MUST be added in this
    CRS — a mismatch silently corrupts edits on commit (degrees written as
    metres) and misplaces the extent. ``latestWkid`` wins (it is the real
    EPSG id); historical Web-Mercator aliases normalise to 3857.
    """
    wkid = None
    if isinstance(spatial_reference, dict):
        wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")
    elif isinstance(spatial_reference, (int, str)):
        wkid = spatial_reference
    try:
        wkid = int(wkid)
    except (TypeError, ValueError):
        return default
    if wkid in _WEB_MERCATOR_WKIDS:
        return "EPSG:3857"
    if wkid <= 0:
        return default
    return "EPSG:{}".format(wkid)


def fmt_bytes(num):
    """Human-readable byte count: ``0 B`` / ``12 KB`` / ``3.4 MB`` / ``1.2 GB``.

    Decimal (1000-based) units to match how the platform reports quotas. One
    decimal place for MB/GB/TB, none for B/KB. A non-numeric/negative input
    formats as ``0 B`` so the overview line never shows junk.
    """
    try:
        value = float(num)
    except (TypeError, ValueError):
        value = 0.0
    if value < 0:
        value = 0.0
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    idx = 0
    while value >= 1000 and idx < len(units) - 1:
        value /= 1000.0
        idx += 1
    if idx <= 1:  # B and KB read better as whole numbers
        return "{:.0f} {}".format(value, units[idx])
    return "{:.1f} {}".format(value, units[idx])


def storage_overview(storage):
    """One muted account-row line from a ``/hub/storage`` envelope (#677).

    * a set quota -> ``"<used> of <quota> used (<pct>%)"``
    * ``quotaBytes == 0`` (unlimited) -> ``"<used> used"``
    * no/blank envelope -> ``""`` (the caller hides the line)

    The percent prefers the server's ``percent`` and falls back to a computed
    ``used/quota`` so the line is right even if the server omits it.
    """
    if not isinstance(storage, dict):
        return ""
    used = storage.get("usedBytes")
    if used is None:
        return ""
    quota = storage.get("quotaBytes") or 0
    try:
        quota = int(quota)
    except (TypeError, ValueError):
        quota = 0
    if quota <= 0:
        return "{} used".format(fmt_bytes(used))
    pct = storage.get("percent")
    if not isinstance(pct, (int, float)):
        try:
            pct = (float(used) / float(quota)) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            pct = 0.0
    return "{} of {} used ({:.0f}%)".format(fmt_bytes(used), fmt_bytes(quota), pct)


def _short(obj, limit=400):
    try:
        text = obj if isinstance(obj, str) else json.dumps(obj)
    except (TypeError, ValueError):
        text = str(obj)
    return text[:limit]


def _parse(payload, status):
    text = payload.decode("utf-8", "replace") if payload else ""
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if status and status >= 400:
        raise GeoiError(
            _error_message(data) or "Request failed (HTTP {}).".format(status),
            status=status,
            code=_error_code(data),
        )
    if isinstance(data, dict):
        if data.get("success") is False or "error" in data:
            raise GeoiError(
                _error_message(data) or "Request failed.",
                status=status,
                code=_error_code(data),
            )
        return data
    if data is None:
        return {}
    return data


def _error_message(data):
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("details")
        # A flat `error` is a stable CODE (e.g. "feature_off"); prefer the
        # server's sibling human `message` over echoing the bare token.
        if isinstance(data.get("message"), str):
            return data["message"]
        if isinstance(err, str):
            return err
    return None


def _error_code(data):
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("code")
        # The platform emits `error` as a flat string code
        # (api/raster.php: 'error' => 'feature_off') — return it as the code.
        if isinstance(err, str):
            return err
    return None


def encode_multipart_form(fields, files=None):
    """Encode multipart/form-data with proper text vs file parts.

    `fields` (name -> str) are plain form fields (no filename) so they land
    in PHP's $_POST; `files` (name -> (filename, data, content_type)) carry a
    filename + Content-Type so they land in $_FILES.
    """
    boundary = "----geoiqgis" + uuid.uuid4().hex
    crlf = b"\r\n"
    bb = boundary.encode("ascii")
    out = []
    for name, value in (fields or {}).items():
        out.append(b"--" + bb)
        out.append(('Content-Disposition: form-data; name="{}"'.format(name)).encode("utf-8"))
        out.append(b"")
        out.append(value.encode("utf-8") if isinstance(value, str) else value)
    for name, (filename, data, ctype) in (files or {}).items():
        if isinstance(data, str):
            data = data.encode("utf-8")
        out.append(b"--" + bb)
        out.append(('Content-Disposition: form-data; name="{}"; filename="{}"'.format(
            name, filename)).encode("utf-8"))
        out.append(("Content-Type: " + ctype).encode("ascii"))
        out.append(b"")
        out.append(data)
    out.append(b"--" + bb + b"--")
    out.append(b"")
    return crlf.join(out), "multipart/form-data; boundary=" + boundary


def encode_multipart(files):
    """Encode a single-part-per-field multipart/form-data body.

    `files` maps field name -> (filename, data, content_type).
    Returns (body_bytes, content_type_header).
    """
    boundary = "----geoiqgis" + uuid.uuid4().hex
    crlf = b"\r\n"
    out = []
    bb = boundary.encode("ascii")
    for field, (filename, data, ctype) in files.items():
        if isinstance(data, str):
            data = data.encode("utf-8")
        disp = 'Content-Disposition: form-data; name="{}"; filename="{}"'.format(
            field, filename
        )
        out.append(b"--" + bb)
        out.append(disp.encode("utf-8"))
        out.append(("Content-Type: " + ctype).encode("ascii"))
        out.append(b"")
        out.append(data)
    out.append(b"--" + bb + b"--")
    out.append(b"")
    body = crlf.join(out)
    return body, "multipart/form-data; boundary=" + boundary
