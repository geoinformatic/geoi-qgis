"""geoi QGIS plugin — controller that wires the panel, client and tasks."""

import json
import os

from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QInputDialog, QMessageBox

from . import convert, settings
from .auth import SessionStore
from .geoi_client import (
    GeoiClient,
    GeoiError,
    epsg_from_spatial_reference,
    friendly_error,
)
from .gui.browser_panel import GeoiPanel
from .gui.dialogs import (
    FeedbackDialog,
    MoveToFolderDialog,
    PublishDialog,
    PublishRasterDialog,
    SaveProjectDialog,
    SettingsDialog,
    ShareDialog,
)
from .tasks import (
    SIGNIN_DISABLED,
    ActionTask,
    CatalogTask,
    PublishTask,
    RasterPublishTask,
    SaveProjectTask,
    SignInTask,
)

_ICON = os.path.join(os.path.dirname(__file__), "icon.svg")


class GeoiPlugin:
    def __init__(self, iface):
        self.iface = iface
        self._store = SessionStore()
        self._client = GeoiClient(base_url=settings.base_url(), log=self._log_msg)
        self._panel = None
        self._action = None
        self._feedback_action = None
        self._tasks = set()
        self._groups = []

    def _log_msg(self, msg):
        """Write a diagnostic line to the 'geoi' tab of the Log Messages panel."""
        try:
            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(str(msg), "geoi")
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------- lifecycle
    def initGui(self):  # noqa: N802 - QGIS-required name
        self._panel = GeoiPanel(self)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._panel)
        self._panel.hide()

        icon = QIcon(_ICON) if os.path.exists(_ICON) else QIcon()
        self._action = QAction(icon, "geoi", self.iface.mainWindow())
        self._action.setCheckable(True)
        self._action.toggled.connect(self._panel.setVisible)
        self._panel.visibilityChanged.connect(self._action.setChecked)
        self.iface.addToolBarIcon(self._action)
        self.iface.addPluginToWebMenu("geoi", self._action)

        # "Send feedback…" — always available (works signed out too).
        self._feedback_action = QAction(icon, "Send feedback…", self.iface.mainWindow())
        self._feedback_action.triggered.connect(self.open_feedback)
        self.iface.addPluginToWebMenu("geoi", self._feedback_action)

        self._restore_session()

    def unload(self):
        if self._action is not None:
            self.iface.removeToolBarIcon(self._action)
            self.iface.removePluginWebMenu("geoi", self._action)
        if self._feedback_action is not None:
            self.iface.removePluginWebMenu("geoi", self._feedback_action)
        if self._panel is not None:
            self.iface.removeDockWidget(self._panel)
            self._panel.deleteLater()

    # --------------------------------------------------------------- helpers
    def _run(self, task):
        self._tasks.add(task)
        QgsApplication.taskManager().addTask(task)

    def _done(self, task):
        self._tasks.discard(task)

    def _warn(self, title, text):
        QMessageBox.warning(self.iface.mainWindow(), title, text)

    def _info(self, text):
        # pushInfo takes (title, message) — no level enum, so it's safe across
        # QGIS 3 (Qt5) and QGIS 4 (Qt6, where level must be a Qgis enum, not int).
        try:
            self.iface.messageBar().pushInfo("geoi", text)
        except Exception:  # noqa: BLE001 - messaging must never break a flow
            pass

    def _signed_in(self):
        return bool(self._client.token)

    # --------------------------------------------------------------- session
    def _restore_session(self):
        """SILENTLY re-establish a previously-signed-in session at startup.

        Startup is signed-out and IDLE: a restored token may re-establish the
        session WITHOUT any network call, browser or prompt. We do NOT
        auto-``refresh()`` here — that fired a content load (and, on a stale
        token, a blocking warning modal) the instant QGIS opened, which felt
        like a forced sign-in. The content loads only when the user explicitly
        opens the panel and presses Refresh, or signs in. No token ⇒ the
        plugin simply stays signed-out and idle.
        """
        token = self._store.load_token()
        if not token:
            return
        self._client.set_token(token)
        # Rebuild the bearer authcfg from the RESTORED token. Without this a
        # private add relied on a possibly-stale authcfg from a prior session
        # (a different/expired baked-in bearer) — the layer then 401s and shows
        # the "make sure you are signed in" error even though we are.
        self._sync_header_authcfg()
        self._panel.set_signed_in(settings.session_user())

    def _sync_header_authcfg(self):
        """(Re)build the bearer authcfg from the CURRENT live token and persist
        its id, returning that id (or "" when not signed in).

        The ``APIHeader`` auth config bakes the literal ``Authorization:
        Bearer <token>`` in at creation, so it must be rebuilt whenever the
        token changes (sign-in, paste-code, session restore) — otherwise a
        private feature-service add injects a stale bearer and 401s.

        Failure-safety: a TRANSIENT failure to (re)build (e.g. the auth DB is
        momentarily locked) returns "" but MUST NOT wipe a good stored
        authcfg — overwriting it with "" is what made private adds break after
        a flaky session. We only persist a NON-empty id; an empty result keeps
        whatever id was already stored.
        """
        token = self._client.token
        if not token:
            return ""
        authcfg = self._store.ensure_header_authcfg(token, settings.header_authcfg())
        if authcfg:
            settings.set_header_authcfg(authcfg)
            return authcfg
        # Keep the previously-stored id rather than clobbering it with "".
        return settings.header_authcfg()

    def open_settings(self):
        dlg = SettingsDialog(self.iface.mainWindow())
        if dlg.exec():
            self._client.base_url = GeoiClient(base_url=settings.base_url()).base_url

    # ----------------------------------------------------------------- auth
    def sign_in(self):
        # Open the browser PROMPTLY. The previous code called
        # /auth/providers SYNCHRONOUSLY on the UI thread first, so a slow
        # round-trip (up to the 30 s client timeout) froze QGIS and delayed the
        # browser by ~15 s. The provider availability check now runs INSIDE the
        # background task with a SHORT timeout — the common case (sign-in
        # enabled) opens the browser immediately; sign-in being disabled is
        # surfaced gracefully afterwards. We never hardcode a provider list;
        # the choice is rendered on the hosted desktop-signin page.
        self._panel.set_busy("Opening your browser to sign in…")

        def done(ok, payload):
            if ok:
                self._finish_sign_in(payload.get("user", {}))
            else:
                self._panel.set_busy("")
                # A "sign-in disabled" verdict from the task is a definitive
                # message, not a paste prompt — show it plainly.
                if isinstance(payload, str) and payload.startswith(SIGNIN_DISABLED):
                    self._warn("geoi sign-in", payload[len(SIGNIN_DISABLED):])
                else:
                    self._offer_paste(payload)

        task = SignInTask(self._client, done)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    def _finish_sign_in(self, user):
        self._store.save_token(self._client.token)
        settings.set_session_user(user)
        # Rebuild the bearer authcfg from the freshly-issued token. Uses the
        # shared helper so a transient build failure never wipes a good stored
        # authcfg (the old code persisted "" unconditionally).
        self._sync_header_authcfg()
        self._panel.set_signed_in(user)
        self._info("Signed in as {}".format(user.get("email", "")))
        self.refresh()

    def _offer_paste(self, reason):
        """Fallback for locked-down machines: paste the one-time code."""
        prompt = (str(reason) + "\n\n") if reason else ""
        code, ok = QInputDialog.getText(
            self.iface.mainWindow(),
            "geoi sign-in",
            prompt + "If your browser shows a one-time sign-in code, paste it here:",
        )
        if not ok or not code.strip():
            return
        self._client.set_token(code.strip())
        try:
            user = self._client.me().get("user", {})
        except GeoiError as exc:
            self._client.set_token(None)
            self._warn("geoi sign-in", "That code was not accepted:\n" + str(exc))
            return
        self._finish_sign_in(user)

    def sign_out(self):
        try:
            self._client.signout()
        except GeoiError:
            pass
        self._store.clear_token()
        self._store.remove_authcfg(settings.header_authcfg())
        settings.set_header_authcfg("")
        settings.set_session_user(None)
        self._panel.set_signed_out()
        self._info("Signed out.")

    # -------------------------------------------------------------- browse
    def refresh(self):
        if not self._signed_in():
            return
        self._panel.set_busy("Loading your content…")

        def done(ok, payload):
            if ok:
                self._groups = payload.get("groups", [])
                payload["ownerId"] = (settings.session_user() or {}).get("id")
                self._panel.populate(payload)
                # Storage overview (#677): fetched fail-soft in the same task;
                # a None envelope hides the line, never blocks the content load.
                self._panel.set_storage(payload.get("storage"))
            else:
                self._panel.set_busy("")
                self._warn("geoi", "Could not load your content:\n" + str(payload))

        task = CatalogTask(self._client, done)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    # ------------------------------------------------- folders / CRUD / move
    def _run_action(self, description, fn, on_ok):
        """Run a one-shot client call off the GUI thread, then refresh."""
        def done(ok, payload):
            if ok:
                if on_ok:
                    on_ok(payload)
                self.refresh()
            else:
                self._warn("geoi", str(payload))

        task = ActionTask(description, fn, done)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    # --------------------------------------------------------------- feedback
    def _plugin_version(self):
        path = os.path.join(os.path.dirname(__file__), "metadata.txt")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("version="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def _system_info(self):
        """A small, relevant set of debug info — shown to the user before send."""
        import platform

        info = {}
        try:
            info["plugin_version"] = self._plugin_version()
        except Exception:  # noqa: BLE001
            pass
        try:
            from qgis.core import Qgis
            info["qgis"] = Qgis.QGIS_VERSION
        except Exception:  # noqa: BLE001
            pass
        try:
            from qgis.PyQt.QtCore import QT_VERSION_STR
            info["qt"] = QT_VERSION_STR
        except Exception:  # noqa: BLE001
            pass
        try:
            info["python"] = platform.python_version()
        except Exception:  # noqa: BLE001
            pass
        try:
            info["os"] = platform.platform()
        except Exception:  # noqa: BLE001
            pass
        try:
            from qgis.PyQt.QtCore import QLocale
            info["locale"] = QLocale().name()
        except Exception:  # noqa: BLE001
            pass
        try:
            user = settings.session_user() or {}
            if user.get("email"):
                info["account"] = user.get("email")
        except Exception:  # noqa: BLE001
            pass
        try:
            info["base_url"] = settings.base_url()
        except Exception:  # noqa: BLE001
            pass
        return info

    def open_feedback(self):
        info = self._system_info()
        user = settings.session_user() or {}
        dlg = FeedbackDialog(system_info=info, default_email=user.get("email", ""),
                             parent=self.iface.mainWindow())
        if not dlg.exec():
            return
        sysinfo = json.dumps(info)
        category = dlg.category()
        title = dlg.title()
        body = dlg.description()
        email = dlg.email()
        attachment = dlg.attachment_path()
        version = info.get("plugin_version", "")

        def work():
            return self._client.send_feedback(
                category=category, title=title, body=body, email=email,
                source="qgis", app_version=version, sysinfo=sysinfo,
                attachment_path=attachment)

        def done(ok, payload):
            if ok:
                QMessageBox.information(
                    self.iface.mainWindow(), "geoi",
                    "Thanks — your feedback was sent to the geoi team.")
            else:
                self._warn("geoi", str(payload))

        task = ActionTask("sending feedback", work, done)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    def create_folder(self, parent_id):
        if not self._signed_in():
            return
        title, ok = QInputDialog.getText(
            self.iface.mainWindow(), "New folder", "Folder name:"
        )
        title = (title or "").strip()
        if not ok or not title:
            return
        self._run_action(
            "creating folder",
            lambda: self._client.create_folder(title, parent_id),
            lambda _p: self._info("Folder '{}' created.".format(title)),
        )

    def share_item(self, kind, payload):
        """Set visibility and reconcile group shares for a service or project."""
        if not self._signed_in():
            return
        shared_ids = [g.get("id") for g in payload.get("groups", [])]
        current = payload.get("visibility", "private")
        dlg = ShareDialog(
            "Share '{}'".format(_title(payload)), self._groups,
            shared_ids, current, self.iface.mainWindow(),
        )
        if not dlg.exec():
            return
        vis = dlg.visibility()
        chosen = set(dlg.selected_group_ids())
        before = set(shared_ids)

        def work():
            if kind == "service":
                name = payload.get("name")
                self._client.update_service(name, visibility=vis)
                if vis == "groups":
                    for gid in chosen - before:
                        self._client.share_service_with_group(name, gid)
                    for gid in before - chosen:
                        self._client.unshare_service_group(name, gid)
            else:
                pid = payload.get("id")
                self._client.update_project(pid, visibility=vis)
                if vis == "groups":
                    for gid in chosen - before:
                        self._client.share_project_with_group(pid, gid)
                    for gid in before - chosen:
                        self._client.unshare_project_group(pid, gid)
            return True

        self._run_action("updating sharing", work,
                         lambda _p: self._info("Sharing updated."))

    def copy_service_url(self, payload):
        name = payload.get("name")
        if not name:
            return
        url = self._client.feature_server_url(name)
        try:
            QApplication.clipboard().setText(url)
            self._info("Feature Service URL copied:\n" + url)
        except Exception:  # noqa: BLE001
            self._info(url)

    def open_web_app(self):
        try:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(self._client.base_url))
        except Exception:  # noqa: BLE001
            self._warn("geoi", "Could not open the browser.")

    def tile_deeplink_url(self, payload):
        """Build the web-app deep link that LOADS a tile service (#8).

        ``<base_url>/?addtile=<id>`` so the web receiver adds the tile service
        to the map; for a NON-public service the stable ``shareToken`` is
        appended as ``&ttoken=<token>`` so it loads without a sign-in. Reuses
        ``tile_service()`` to read the id + shareToken + visibility. Returns
        ``(url, None)`` or ``("", error)``.
        """
        sid = (payload or {}).get("id")
        if sid in (None, ""):
            return "", "This tile service has no id."
        try:
            detail = self._client.tile_service(sid)
        except GeoiError as exc:
            return "", friendly_error(exc)
        # Prefer the freshly-read detail's id; fall back to the summary id.
        tid = detail.get("id", sid)
        url = "{base}/?addtile={id}".format(
            base=self._client.base_url, id=_quote(str(tid)))
        visibility = detail.get("visibility") or payload.get("visibility")
        token = detail.get("shareToken")
        if token and visibility != "public":
            url += "&ttoken=" + _quote(str(token))
        return url, None

    def open_tile_in_web_app(self, payload):
        """Open a tile service in the geoi web app via a deep link that loads
        it (#8) — not just the bare root. Mirrors the ``/s/<code>`` flow."""
        url, err = self.tile_deeplink_url(payload)
        if err:
            return self._warn("geoi", "Could not read the tile service:\n" + err)
        try:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(url))
            self._info("Opening the tile service in the geoi web app…")
        except Exception:  # noqa: BLE001
            self._warn("geoi", "Could not open the browser.")

    # ----------------------------------------------------- tile services
    _TILE_FORMAT_LABEL = {"xyz": "XYZ", "wmts": "WMTS", "pmtiles": "PMTiles"}

    def _tile_format_urls(self, payload):
        """Fetch a tile service's per-format URLs (absolute + tokenized).

        Returns ``({xyz, wmts, pmtiles}, None)`` on success or ``({}, error)``
        so the caller can show a friendly message.
        """
        sid = payload.get("id")
        if sid in (None, ""):
            return {}, "This tile service has no id."
        try:
            detail = self._client.tile_service(sid)
        except GeoiError as exc:
            return {}, friendly_error(exc)
        return self._client.tile_format_urls(detail), None

    def copy_tile_url(self, payload, fmt):
        """Copy a tile service's XYZ / WMTS / PMTiles URL to the clipboard.

        Mirrors ``copy_service_url`` — fetch the detail, build the absolute,
        tokenized URL for ``fmt`` and put it on the clipboard.
        """
        urls, err = self._tile_format_urls(payload)
        if err:
            return self._warn("geoi", "Could not read the tile service:\n" + err)
        url = urls.get(fmt)
        label = self._TILE_FORMAT_LABEL.get(fmt, fmt.upper())
        if not url:
            return self._warn(
                "geoi", "This tile service has no {} URL.".format(label))
        try:
            QApplication.clipboard().setText(url)
            self._info("{} URL copied:\n{}".format(label, url))
        except Exception:  # noqa: BLE001
            self._info(url)

    def share_tile_service(self, payload):
        """Set a tile service's visibility and copy its tokenized share URL.

        Mirrors ``share_item`` for feature services, but tile services have NO
        per-group share endpoint: the share URL is the absolute xyz/wmts/pmtiles
        URL plus the stable ``shareToken`` (already appended by ``copy_tile_url``
        / ``tile_format_urls`` for a non-public service). So this only changes
        the visibility, then copies the XYZ share URL for the user to paste.
        """
        if not self._signed_in():
            return
        sid = payload.get("id")
        if sid in (None, ""):
            return self._warn("geoi", "This tile service has no id.")
        current = payload.get("visibility", "private")
        # No group reconciliation for tiles — the dialog's group list is unused;
        # pass the user's groups so "Shared with a group" is offered when valid.
        dlg = ShareDialog(
            "Share '{}'".format(_title(payload)), self._groups,
            [], current, self.iface.mainWindow(),
        )
        if not dlg.exec():
            return
        vis = dlg.visibility()

        def work():
            self._client.set_tile_service_visibility(sid, vis)
            return True

        def on_ok(_p):
            # Re-read the detail so the (now-correct) shareToken/visibility build
            # the right URL, then copy the XYZ link the user pastes into QGIS.
            try:
                detail = self._client.tile_service(sid)
                urls = self._client.tile_format_urls(detail)
            except GeoiError:
                urls = {}
            url = urls.get("xyz")
            if url:
                try:
                    QApplication.clipboard().setText(url)
                    self._info("Visibility set to {}. Share URL copied:\n{}".format(
                        vis, url))
                    return
                except Exception:  # noqa: BLE001
                    pass
            self._info("Visibility set to {}.".format(vis))

        self._run_action("updating tile sharing", work, on_ok)

    def add_tile_layer(self, payload):
        """Add a tile service to the map as a native XYZ raster layer.

        Low-risk: a standard ``QgsRasterLayer(uri, name, 'wms')`` with a
        ``type=xyz&url=…`` URI — QGIS's own XYZ provider. Falls back to a
        copy hint if QGIS can't load it (the copy actions are the must-have).

        PMTiles finding: there is intentionally NO ``add_tile_pmtiles_layer``.
        QGIS's native PMTiles support (≥3.32) is for VECTOR tiles only; a
        hosted RASTER PMTiles archive cannot be added reliably by URL across
        the QGIS versions we support (the GDAL PMTiles driver targets MVT, and
        ``/vsicurl/`` raster-PMTiles is neither stable nor version-portable).
        So we keep the rock-solid XYZ/WMTS add paths plus *Copy PMTiles URL*
        rather than ship a broken action.
        """
        urls, err = self._tile_format_urls(payload)
        if err:
            return self._warn("geoi", "Could not read the tile service:\n" + err)
        xyz = urls.get("xyz")
        if not xyz:
            return self._warn("geoi", "This tile service has no XYZ URL.")
        name = _title(payload)
        # The XYZ provider wants the {z}/{x}/{y} template percent-encoded in
        # the url= parameter; QgsDataSourceUri handles the encoding for us.
        try:
            from qgis.core import QgsDataSourceUri

            uri = QgsDataSourceUri()
            uri.setParam("type", "xyz")
            uri.setParam("url", xyz)
            zmin = payload.get("minZoom")
            zmax = payload.get("maxZoom")
            if zmin is not None:
                uri.setParam("zmin", str(int(zmin)))
            if zmax is not None:
                uri.setParam("zmax", str(int(zmax)))
            source = bytes(uri.encodedUri()).decode("utf-8")
        except Exception:  # noqa: BLE001 - fall back to a hand-built URI
            from urllib.parse import quote

            source = "type=xyz&url=" + quote(xyz, safe="")
        layer = QgsRasterLayer(source, name, "wms")
        if layer.isValid():
            # Tile services go to the BOTTOM — rasters sit beneath vectors.
            self._add_layer_to_toc(layer, top=False)
            self._zoom_to_layers([layer])
            self._info("Added tile service '{}' as an XYZ layer.".format(name))
        else:
            self._warn(
                "geoi",
                "QGIS could not load the XYZ layer. Copy the XYZ URL and add it "
                "via Layer → Add Layer → Add XYZ Layer instead.",
            )

    def add_tile_wmts_layer(self, payload):
        """Add a tile service to the map as a native WMTS raster layer.

        Builds the QGIS WMS/WMTS provider URI QGIS's own "Add WMTS Layer"
        dialog produces for a RESTful WMTS: the absolute (token-appended for a
        non-public service) WMTSCapabilities URL plus ``layers``, ``styles``,
        ``tileMatrixSet=GoogleMapsCompatible``, ``format=image/webp``,
        ``crs=EPSG:3857`` and ``contextualWMSLegend=0``. Mirrors the XYZ
        action's valid/fallback pattern — a copy hint if QGIS can't load it.
        """
        urls, err = self._tile_format_urls(payload)
        if err:
            return self._warn("geoi", "Could not read the tile service:\n" + err)
        wmts = urls.get("wmts")
        if not wmts:
            return self._warn("geoi", "This tile service has no WMTS URL.")
        name = _title(payload)
        source = self._wmts_uri(wmts, payload)
        layer = QgsRasterLayer(source, name, "wms")
        if layer.isValid():
            self._add_layer_to_toc(layer, top=False)
            self._zoom_to_layers([layer])
            self._info("Added tile service '{}' as a WMTS layer.".format(name))
        else:
            self._warn(
                "geoi",
                "QGIS could not load the WMTS layer. Copy the WMTS URL and add "
                "it via Layer → Add Layer → Add WMS/WMTS Layer instead.",
            )

    @staticmethod
    def _wmts_uri(capabilities_url, payload):
        """Build the QGIS ``wms`` provider URI for a RESTful WMTS layer.

        The WMTS layer identifier is the service slug (falling back to the
        title); the geoi tile pipeline publishes the ``GoogleMapsCompatible``
        tile matrix set in WebP, EPSG:3857. ``contextualWMSLegend=0`` matches
        what QGIS's own dialog emits. ``QgsDataSourceUri`` percent-encodes the
        capabilities URL for the ``url=`` param.
        """
        layer_id = (payload.get("slug") or payload.get("name")
                    or _title(payload))
        params = {
            "contextualWMSLegend": "0",
            "crs": "EPSG:3857",
            "format": "image/webp",
            "layers": layer_id,
            "styles": "default",
            "tileMatrixSet": "GoogleMapsCompatible",
            "url": capabilities_url,
        }
        try:
            from qgis.core import QgsDataSourceUri

            uri = QgsDataSourceUri()
            for key, value in params.items():
                uri.setParam(key, value)
            return bytes(uri.encodedUri()).decode("utf-8")
        except Exception:  # noqa: BLE001 - fall back to a hand-built URI
            from urllib.parse import quote

            parts = []
            for key, value in params.items():
                enc = quote(value, safe="") if key == "url" else value
                parts.append("{}={}".format(key, enc))
            return "&".join(parts)

    def add_project(self, payload):
        """Add a saved geoi web map (project) to QGIS.

        Investigation: a geoi "project" is a saved web-map DOCUMENT (the
        ``/hub/projects`` package), not an ArcGIS service, so QGIS has no
        native add-by-URL path for it the way a Feature Service has. There is
        also no client flow that materialises a project's referenced layers
        into QGIS today. The most useful SUPPORTED action is therefore to open
        the web map in the geoi web app, where it renders natively — feature
        services and tile services DO add straight to the QGIS TOC (see
        ``add_service`` / ``add_tile_layer``). If a future endpoint exposes a
        project's referenced services, this is where we'd add them on top.
        """
        pid = (payload or {}).get("id")
        if pid in (None, ""):
            return self.open_web_app()
        try:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices

            url = "{base}/?project={pid}".format(
                base=self._client.base_url,
                pid=_quote(str(pid)),
            )
            QDesktopServices.openUrl(QUrl(url))
            self._info(
                "Opened web map '{}' in the geoi web app.".format(_title(payload)))
        except Exception:  # noqa: BLE001
            self._warn("geoi", "Could not open the web map in the browser.")

    def commit_rename(self, kind, payload, new_name):
        """Persist an inline rename of a service title, project name or folder."""
        if not self._signed_in() or not new_name:
            return self.refresh()
        if kind == "service":
            name = payload.get("name")
            self._run_action(
                "renaming service",
                lambda: self._client.update_service(name, title=new_name),
                None,
            )
        elif kind == "project":
            pid = payload.get("id")
            self._run_action(
                "renaming project",
                lambda: self._client.update_project(pid, name=new_name),
                None,
            )
        elif kind == "folder":
            fid = payload.get("id")
            self._run_action(
                "renaming folder",
                lambda: self._client.rename_folder(fid, new_name),
                None,
            )
        elif kind == "tile":
            sid = payload.get("id")
            self._run_action(
                "renaming tile service",
                lambda: self._client.rename_tile_service(sid, new_name),
                None,
            )

    def delete_folder(self, folder_id, title):
        if not self._signed_in():
            return
        if QMessageBox.question(
            self.iface.mainWindow(), "Delete folder",
            "Delete folder '{}'? Its contents move back to the root.".format(title),
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_action(
            "deleting folder",
            lambda: self._client.delete_folder(folder_id, force=True),
            lambda _p: self._info("Folder '{}' deleted.".format(title)),
        )

    def move_item(self, kind, payload, folder_id):
        if not self._signed_in():
            return
        if kind == "service":
            name = payload.get("name")
            self._run_action(
                "moving service",
                lambda: self._client.set_service_folder(name, folder_id),
                None,
            )
        elif kind == "project":
            pid = payload.get("id")
            self._run_action(
                "moving project",
                lambda: self._client.set_project_folder(pid, folder_id),
                None,
            )
        elif kind == "tile":
            sid = payload.get("id")
            self._run_action(
                "moving tile service",
                lambda: self._client.move_tile_service(sid, folder_id),
                None,
            )
        elif kind == "folder":
            fid = payload.get("id")
            if fid and fid != folder_id:
                self._run_action(
                    "moving folder",
                    lambda: self._client.move_folder(fid, folder_id),
                    None,
                )

    def move_item_pick(self, kind, payload):
        if not self._signed_in():
            return
        current = payload.get("folderId")
        dlg = MoveToFolderDialog(self._panel.folders, current, self.iface.mainWindow())
        if not dlg.exec():
            return
        self.move_item(kind, payload, dlg.selected_folder_id())

    def delete_item(self, kind, payload):
        if not self._signed_in():
            return
        label = _title(payload)
        if QMessageBox.question(
            self.iface.mainWindow(), "Delete {}".format(kind),
            "Delete {} '{}'? This cannot be undone.".format(kind, label),
        ) != QMessageBox.StandardButton.Yes:
            return
        if kind == "service":
            self._run_action(
                "deleting service",
                lambda: self._client.delete_service(payload.get("name")),
                lambda _p: self._info("Service '{}' deleted.".format(label)),
            )
        elif kind == "project":
            self._run_action(
                "deleting project",
                lambda: self._client.delete_project(payload.get("id")),
                lambda _p: self._info("Project '{}' deleted.".format(label)),
            )
        elif kind == "tile":
            self._run_action(
                "deleting tile service",
                lambda: self._client.delete_tile_service(payload.get("id")),
                lambda _p: self._info("Tile service '{}' deleted.".format(label)),
            )

    # --------------------------------------------------------- add service
    def add_service(self, service):
        name = service.get("name")
        if not name:
            return
        if service.get("visibility") == "public":
            authcfg = ""
        else:
            # Rebuild the bearer authcfg from the CURRENT live token right
            # before adding, so a private layer never attaches a stale/expired
            # baked-in bearer (the cause of the "make sure you are signed in"
            # error on an otherwise valid session).
            authcfg = self._sync_header_authcfg()
            if self._signed_in() and not authcfg:
                return self._warn(
                    "geoi",
                    "Your geoi session is missing its sign-in token. Sign out "
                    "and sign in again, then add the layer.",
                )
        try:
            info = self._client.feature_server_info(name)
        except GeoiError as exc:
            self._warn("geoi", "Could not read the service:\n" + str(exc))
            return
        layers = info.get("layers", [])
        if not layers:
            self._info("Service '{}' has no layers.".format(name))
            return
        # Add the layer in the service's OWN spatial reference. Hardcoding
        # EPSG:4326 against a Web-Mercator (3857) service silently corrupts
        # edits on commit and misplaces the extent — see
        # epsg_from_spatial_reference.
        crs = epsg_from_spatial_reference(info.get("spatialReference"))
        base = self._client.feature_server_url(name)
        added = []
        for layer in layers:
            lid = layer.get("id", 0)
            lname = "{} — {}".format(service.get("title") or name, layer.get("name", lid))
            uri = "crs='{}' url='{}/{}'".format(crs, base, lid)
            if authcfg:
                uri += " authcfg='{}'".format(authcfg)
            vlayer = QgsVectorLayer(uri, lname, "arcgisfeatureserver")
            if vlayer.isValid():
                # Feature services go to the TOP of the layer tree — vectors
                # sit above rasters in the natural GIS stacking.
                self._add_layer_to_toc(vlayer, top=True)
                added.append(vlayer)
        if added:
            self._zoom_to_layers(added)
            self._info("Added {} layer(s) from '{}'.".format(len(added), name))
        elif not self._signed_in():
            self._warn(
                "geoi",
                "QGIS could not load the layers. This is a private service — "
                "sign in to geoi first, then add it.",
            )
        else:
            self._warn(
                "geoi",
                "QGIS could not load the layers from '{}'. You are signed in and "
                "the bearer token was attached, so this is likely a transient "
                "server error — try again, or check the service in the geoi web "
                "app.".format(name),
            )

    def _add_layer_to_toc(self, layer, *, top):
        """Add ``layer`` to the project and place it at the TOP or BOTTOM of
        the layer tree (table of contents).

        QGIS's ``addMapLayer(layer)`` (the default) ALSO inserts it at the top
        of the tree, so to control ordering we register the layer WITHOUT
        adding it to the tree (``addMapLayer(layer, False)``) and then insert
        it ourselves: index 0 for the top (vectors above), append for the
        bottom (rasters below). Falls back to the plain add if the tree API is
        unavailable.
        """
        project = QgsProject.instance()
        try:
            root = project.layerTreeRoot()
            project.addMapLayer(layer, False)  # register, but don't auto-place
            if top:
                root.insertLayer(0, layer)
            else:
                root.addLayer(layer)  # append at the bottom
        except Exception:  # noqa: BLE001 - never fail to add over ordering
            project.addMapLayer(layer)

    def _zoom_to_layers(self, layers):
        """Repaint the canvas and frame the freshly-added layers.

        The ArcGIS Feature Service provider loads features asynchronously and
        QGIS does not always repaint after ``addMapLayer`` — without this the
        layers only appear once the user pans. We also frame the combined
        extent so the map jumps to the right place.
        """
        try:
            from qgis.core import (
                QgsCoordinateTransform,
                QgsProject,
                QgsRectangle,
            )

            canvas = self.iface.mapCanvas()
            dest_crs = canvas.mapSettings().destinationCrs()
            project = QgsProject.instance()
            box = QgsRectangle()
            box.setMinimal()
            for vlayer in layers:
                vlayer.updateExtents()
                extent = vlayer.extent()
                if extent.isEmpty():
                    continue
                xform = QgsCoordinateTransform(vlayer.crs(), dest_crs, project)
                box.combineExtentWith(xform.transformBoundingBox(extent))
            if not box.isEmpty():
                box.scale(1.05)
                canvas.setExtent(box)
            canvas.refresh()
        except Exception:  # noqa: BLE001 - framing must never break adding
            try:
                self.iface.mapCanvas().refresh()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------- publish / save
    def _project_vector_layers(self):
        out = []
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
                out.append(layer)
        return out

    def _layers_info(self, layers):
        """A per-layer {id, name, summary} for the publish/save dialogs, so the
        user sees the name + how each layer is styled before sending."""
        from . import convert, style

        infos = []
        for layer in layers:
            try:
                geom = convert.geoi_geometry_from_code(layer.geometryType())
                sym = style.layer_symbology(layer, geom)
                count = layer.featureCount()
                summary = style.describe(sym, count if count is not None and count >= 0 else None)
            except Exception:  # noqa: BLE001 - the summary must never block publish
                summary = ""
            infos.append({"id": layer.id(), "name": layer.name(), "summary": summary})
        return infos

    def _collect(self, layer_ids):
        """Build the geoi FeatureCollection from the given QGIS layer ids."""
        registry = QgsProject.instance()
        layer_dicts, features = [], []
        for lid in layer_ids:
            layer = registry.mapLayer(lid)
            if layer is None:
                continue
            ldict, lfeatures = convert.qgis_layer_to_geoi(layer)
            layer_dicts.append(ldict)
            features.extend(lfeatures)
        return layer_dicts, features

    def publish_project(self):
        if not self._signed_in():
            return self._warn("geoi", "Please sign in first.")
        layers = self._project_vector_layers()
        if not layers:
            return self._warn("geoi", "There are no vector layers to publish.")
        dlg = PublishDialog(
            self._layers_info(layers), default_name=_project_name(),
            groups=self._groups, parent=self.iface.mainWindow(),
        )
        if not dlg.exec():
            return
        ids = dlg.selected_layer_ids()
        if not ids:
            return self._warn("geoi", "Select at least one layer.")
        name = dlg.service_name() or "My layers"
        options = {
            "visibility": dlg.visibility(),
            "editable": dlg.editable(),
            "group_ids": dlg.selected_group_ids(),
        }
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            layer_dicts, features = self._collect(ids)
            fc = convert.build_feature_collection(layer_dicts, features, project_name=name)
            data = json.dumps(fc).encode("utf-8")
        finally:
            QApplication.restoreOverrideCursor()

        def done(ok, payload):
            if ok:
                svc = payload.get("name", name)
                vis = payload.get("visibility", options["visibility"])
                url = self._client.feature_server_url(svc)
                self._log_msg("Published service '{}' ({}) -> {}".format(svc, vis, url))
                QMessageBox.information(
                    self.iface.mainWindow(),
                    "geoi — published",
                    "Service '{}' was created ({}{}).\n\nIt is now in your geoi "
                    "content (and visible in the geoi web app).\n\n{}".format(
                        svc, vis,
                        ", editable" if payload.get("editable") else "", url),
                )
                self.refresh()
            else:
                self._warn("geoi publish failed", str(payload))

        task = PublishTask(
            self._client, name + ".geojson", data, "application/geo+json", options, done
        )
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    def save_project(self):
        if not self._signed_in():
            return self._warn("geoi", "Please sign in first.")
        layers = self._project_vector_layers()
        if not layers:
            return self._warn("geoi", "There are no vector layers to save.")
        dlg = SaveProjectDialog(
            default_name=_project_name(), layers_info=self._layers_info(layers),
            parent=self.iface.mainWindow(),
        )
        if not dlg.exec():
            return
        name = dlg.project_name() or "QGIS project"
        visibility = dlg.visibility()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ids = [layer.id() for layer in layers]
            layer_dicts, features = self._collect(ids)
            fc = convert.build_feature_collection(layer_dicts, features, project_name=name)
        finally:
            QApplication.restoreOverrideCursor()

        def done(ok, payload):
            if ok:
                pid = payload.get("id", "")
                self._log_msg("Saved project '{}' (id {}).".format(name, pid))
                QMessageBox.information(
                    self.iface.mainWindow(),
                    "geoi — saved",
                    "Project '{}' was saved to geoi (id {}).\n\nOpen it from the "
                    "geoi web app or this panel.".format(name, pid),
                )
                self.refresh()
            else:
                self._warn("geoi save failed", str(payload))

        task = SaveProjectTask(self._client, name, fc, done, visibility=visibility)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    # ----------------------------------------------------- publish raster
    def _project_raster_layers(self):
        out = []
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and layer.isValid():
                out.append(layer)
        return out

    def _raster_layers_info(self, layers):
        """A per-layer {name, source} for the raster publish dialog. Only file
        sources can be tiled, so layers without one (WMS/XYZ) are skipped."""
        infos = []
        for layer in layers:
            try:
                source = layer.source()
            except Exception:  # noqa: BLE001
                source = ""
            # Tile only local file rasters — a provider URI (wms/xyz) has a
            # scheme or 'url=' and cannot be mosaicked from disk.
            if source and "://" not in source and "url=" not in source \
                    and os.path.exists(source.split("|")[0]):
                infos.append({"name": layer.name(),
                              "source": source.split("|")[0]})
        return infos

    def publish_raster(self):
        if not self._signed_in():
            return self._warn("geoi", "Please sign in first.")
        infos = self._raster_layers_info(self._project_raster_layers())
        dlg = PublishRasterDialog(
            raster_layers_info=infos, default_name=_project_name() + " tiles",
            parent=self.iface.mainWindow(),
        )
        if not dlg.exec():
            return
        sources = dlg.sources()
        if not sources:
            return self._warn(
                "geoi", "Choose at least one raster layer or a folder of GeoTIFFs.")
        name = dlg.tile_name() or "My tiles"

        def done(ok, payload):
            if ok:
                url = payload.get("url", "")
                self._log_msg("Published raster tiles '{}' -> {}".format(name, url))
                QMessageBox.information(
                    self.iface.mainWindow(),
                    "geoi — tiles published",
                    "Raster tiles '{}' were published in Web Mercator "
                    "(EPSG:3857).\n\n{}".format(name, url),
                )
                self.refresh()
            else:
                self._warn("geoi raster publish failed", str(payload))

        self._info("Tiling raster to Web Mercator (EPSG:3857) and uploading…")
        # CRS is forced inside the pipeline — no CRS is passed from here.
        task = RasterPublishTask(self._client, sources, name, done)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)


def _quote(value):
    """URL-encode a single query-parameter value (safe='')."""
    from urllib.parse import quote

    return quote(value, safe="")


def _project_name():
    title = QgsProject.instance().title() or QgsProject.instance().baseName()
    return title or "QGIS project"


def _title(entry):
    return (entry or {}).get("title") or (entry or {}).get("name") \
        or str((entry or {}).get("id", ""))
