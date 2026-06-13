"""
geoi platform REST client.

Pure Python (standard library only) — no QGIS imports and no third-party
dependencies — so it is unit-testable off a QGIS install and trivial to
ship. It speaks the same endpoints the geoi web app uses, under
``<base>/platform/``:

  GET  /auth/config      sign-in availability + Google client ids
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

    def list_groups(self):
        return self._request("GET", "/hub/groups").get("groups", [])

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
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
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
        if isinstance(err, str):
            return err
        if isinstance(data.get("message"), str):
            return data["message"]
    return None


def _error_code(data):
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return data["error"].get("code")
    return None


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
