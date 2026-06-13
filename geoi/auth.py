"""Zero-config sign-in for the QGIS plugin.

The plugin is NOT a Google OAuth client. Instead it reuses the geoi web
app's own Google sign-in: it opens the hosted handoff page
``<base>/desktop-signin.html`` in the system browser, the user signs in with
the same Google button the web app shows (the existing web OAuth client, no
configuration), and the page hands the resulting geoi **session token** back
to the plugin over a LOOPBACK redirect (``http://127.0.0.1:<port>``). A
``state`` nonce ties the response to this request.

The loopback flow is pure standard-library and unit-tested. ``SessionStore``
(encrypted token storage + the bearer auth config the ArcGIS provider uses)
imports qgis lazily.
"""

import http.server
import secrets
import urllib.parse
import webbrowser

SIGNIN_PATH = "/desktop-signin.html"

# QgsAuthManager setting key + the auth-config name for the bearer header.
SESSION_SETTING_KEY = "geoi/session"
HEADER_CONFIG_NAME = "geoi platform (bearer)"


class AuthError(Exception):
    pass


# ------------------------------------------------------------- loopback flow
class _TokenHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required name
        server = self.server
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token = qs.get("token", [None])[0]
        state = qs.get("state", [None])[0]
        ok = bool(token) and state == server.expected_state
        if ok:
            server.result = {"token": token}
        elif "error" in qs:
            server.result = {"error": qs.get("error", ["sign-in failed"])[0]}
        body = (
            "<html><body style='font:16px sans-serif;padding:3rem;text-align:center'>"
            "<h2>geoi</h2><p>{}</p></body></html>"
        ).format(
            "You are signed in. You can close this tab and return to QGIS."
            if ok
            else "Sign-in could not be completed. You can close this tab."
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence default stderr logging
        pass


def build_signin_url(base_url, port, state):
    base = (base_url or "https://geoi.de").rstrip("/")
    query = urllib.parse.urlencode({"port": port, "state": state})
    return "{}{}?{}".format(base, SIGNIN_PATH, query)


def run_web_signin(base_url, open_url=None, timeout=300, is_cancelled=None):
    """Open the hosted sign-in page and capture the session token via loopback.

    Returns the geoi session token (already exchanged by the page).
    `open_url` defaults to the system browser; `is_cancelled` is polled so a
    host UI can abort.
    """
    state = secrets.token_urlsafe(24)
    server = http.server.HTTPServer(("127.0.0.1", 0), _TokenHandler)
    server.expected_state = state
    server.result = None
    server.timeout = 1
    port = server.server_address[1]

    url = build_signin_url(base_url, port, state)
    (open_url or webbrowser.open)(url)

    waited = 0
    try:
        while server.result is None and waited < timeout:
            if is_cancelled and is_cancelled():
                raise AuthError("Sign-in cancelled.")
            server.handle_request()  # one request per second (server.timeout)
            waited += 1
    finally:
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass

    result = server.result
    if not result:
        raise AuthError(
            "Timed out waiting for the browser sign-in. If your browser shows a "
            "one-time code, paste it into QGIS instead."
        )
    if "error" in result:
        raise AuthError("Sign-in failed: {}".format(result["error"]))
    return result["token"]


# --------------------------------------------------------- session storage
class SessionStore:
    """Encrypted session-token storage + the bearer auth config for layers.

    Tokens live in the QGIS authentication database (QgsAuthManager), not in
    plain settings; the per-user display info is kept in plain QSettings.
    """

    def __init__(self):
        self._am = None

    def _auth_manager(self):
        if self._am is None:
            from qgis.core import QgsApplication

            self._am = QgsApplication.authManager()
        return self._am

    # token -------------------------------------------------------------
    def save_token(self, token):
        self._auth_manager().storeAuthSetting(SESSION_SETTING_KEY, token, True)

    def load_token(self):
        value = self._auth_manager().authSetting(SESSION_SETTING_KEY, "", True)
        return value or None

    def clear_token(self):
        try:
            self._auth_manager().removeAuthSetting(SESSION_SETTING_KEY)
        except Exception:  # noqa: BLE001
            pass

    # bearer header auth config (used by the ArcGIS Feature Service layers) --
    def ensure_header_authcfg(self, token, existing_id=""):
        """Create or update an 'APIHeader' auth config injecting the bearer.

        Returns the authcfg id to attach to layer URIs, or "" if the auth
        config could not be created (sign-in must never fail because of this —
        public services still work without it).
        """
        try:
            from qgis.core import QgsApplication, QgsAuthMethodConfig

            am = QgsApplication.authManager()
            config = QgsAuthMethodConfig()
            if existing_id and existing_id in am.configIds():
                am.loadAuthenticationConfig(existing_id, config, True)
            config.setName(HEADER_CONFIG_NAME)
            config.setMethod("APIHeader")
            config.setConfig("Authorization", "Bearer " + token)
            if existing_id and config.id() == existing_id:
                am.updateAuthenticationConfig(config)
                return existing_id
            # storeAuthenticationConfig returns either a bool (mutating the
            # passed config in place) or a (bool, config) tuple, depending on
            # the QGIS/PyQGIS build — handle both.
            res = am.storeAuthenticationConfig(config)
            if isinstance(res, tuple):
                ok = bool(res[0])
                stored = res[1] if len(res) > 1 and hasattr(res[1], "id") else config
            else:
                ok = bool(res)
                stored = config
            new_id = stored.id()
            if ok and new_id:
                return new_id
        except Exception:  # noqa: BLE001 - never break sign-in over this
            pass
        return ""

    def remove_authcfg(self, authcfg_id):
        if not authcfg_id:
            return
        try:
            from qgis.core import QgsApplication

            QgsApplication.authManager().removeAuthenticationConfig(authcfg_id)
        except Exception:  # noqa: BLE001
            pass
