"""geoi QGIS plugin — controller that wires the panel, client and tasks."""

import json
import os
import urllib.parse

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
    tiles3d_friendly_error,
)
from .gui.browser_panel import GeoiPanel
from .gui.dialogs import (
    FeedbackDialog,
    ManageGroupsDialog,
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
    DiscoverTask,
    PublishTask,
    RasterPublishTask,
    SaveProjectTask,
    SignInTask,
    Tiles3dPublishTask,
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
        # security review: diagnostic logging must never break the caller
        except Exception:  # nosec B110
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
        # security review: message-bar notification must never break a flow
        except Exception:  # nosec B110
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

    def search_discover(self, query):
        """Search geoi's PUBLIC content off the GUI thread and render it into
        the panel's tree (Discover mode, WS3c). Public search, so it works
        signed out too; a failure surfaces a warning and clears the busy line.
        """
        def done(ok, payload):
            if ok:
                self._panel.populate_discover(payload, query)
            else:
                self._panel.set_busy("")
                self._warn("geoi",
                           "Could not search public content:\n" + str(payload))

        task = DiscoverTask(self._client, query, done)
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
        # security review: plugin_version is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
            pass
        try:
            from qgis.core import Qgis
            info["qgis"] = Qgis.QGIS_VERSION
        # security review: qgis version is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
            pass
        try:
            from qgis.PyQt.QtCore import QT_VERSION_STR
            info["qt"] = QT_VERSION_STR
        # security review: Qt version is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
            pass
        try:
            info["python"] = platform.python_version()
        # security review: python version is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
            pass
        try:
            info["os"] = platform.platform()
        # security review: OS platform string is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
            pass
        try:
            from qgis.PyQt.QtCore import QLocale
            info["locale"] = QLocale().name()
        # security review: locale name is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
            pass
        try:
            user = settings.session_user() or {}
            if user.get("email"):
                info["account"] = user.get("email")
        # security review: account email is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
            pass
        try:
            info["base_url"] = settings.base_url()
        # security review: base_url is an optional diagnostic field for Send feedback
        except Exception:  # nosec B110
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

    def manage_groups(self):
        """Create / rename / delete groups and add / remove members by email.

        Opens :class:`ManageGroupsDialog` (live, synchronous group management),
        then refreshes the cached group list so the Share dialogs immediately
        see new / renamed / deleted groups — reusing ``refresh``'s groups-load
        path (it sets ``self._groups`` from the reloaded catalogue).
        """
        if not self._signed_in():
            return self._warn("geoi", "Please sign in first.")
        dlg = ManageGroupsDialog(
            self._client, self._groups, self.iface.mainWindow())
        dlg.exec()
        # Sync self._groups off the same load path refresh() uses (line ~255),
        # so ShareDialog sees the changes without stale group choices.
        self.refresh()

    def set_service_editable(self, payload, editable):
        """Toggle a published Feature Service between editable (read/write) and
        read-only AFTER publishing (``update_service(name, editable=…)``)."""
        if not self._signed_in():
            return
        name = payload.get("name")
        if not name:
            return
        self._run_action(
            "updating service editability",
            lambda: self._client.update_service(name, editable=bool(editable)),
            lambda _p: self._info(
                "'{}' is now {}.".format(
                    _title(payload),
                    "editable (read/write)" if editable else "read-only")),
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

    def bulk_share(self, items):
        """Share MANY selected content items at once (PR-B).

        ``items`` is the ``[(kind, payload, shared), …]`` shape from
        ``browser_panel.bulk_shareable`` — already filtered to the OWNED,
        shareable subset. ONE ShareDialog collects a target visibility +
        groups, applied to EVERY item. Sharing is ADDITIVE: no per-item
        pre-selection makes sense across a heterogeneous, mixed-kind
        selection, so ``shared_ids=[]`` and only chosen groups are ADDED —
        nothing already shared is revoked.
        """
        if not self._signed_in() or not items:
            return
        n = len(items)
        # ShareDialog has no subtitle/hint param, so the additive-only
        # semantics live here: shared_ids=[] means "no pre-existing selection",
        # and only the chosen groups are applied per item (never un-shared).
        dlg = ShareDialog(
            "Share {} items".format(n), self._groups,
            [], "private", self.iface.mainWindow(),
        )
        if not dlg.exec():
            return
        vis = dlg.visibility()
        chosen = list(dlg.selected_group_ids()) if vis == "groups" else []

        def work():
            # Sequential (never parallel) — one item at a time; a failure on
            # item N is recorded and the loop CONTINUES to N+1 so a single bad
            # item never hides the rest.
            results = []
            for kind, payload, _shared in items:
                title = _title(payload)
                try:
                    self._bulk_share_one(kind, payload, vis, chosen)
                    results.append((title, True, None))
                except Exception as exc:  # noqa: BLE001 - collected, not raised
                    results.append(
                        (title, False,
                         str(getattr(exc, "message", None) or exc)))
            return results

        self._run_action("updating sharing", work,
                         lambda results: self._on_bulk_shared(results, n))

    def _bulk_share_one(self, kind, payload, vis, chosen):
        """Apply ONE item's visibility + additive group shares, by kind.

        Sets the item's visibility, then (only for ``groups``) shares it with
        each chosen group via that kind's share endpoint. Raises on any client
        error so ``bulk_share``'s loop can record the failure and continue.
        """
        if kind == "service":
            name = payload.get("name")
            self._client.update_service(name, visibility=vis)
            if vis == "groups":
                for gid in chosen:
                    self._client.share_service_with_group(name, gid)
        elif kind == "project":
            pid = payload.get("id")
            self._client.update_project(pid, visibility=vis)
            if vis == "groups":
                for gid in chosen:
                    self._client.share_project_with_group(pid, gid)
        elif kind == "tile":
            sid = payload.get("id")
            self._client.set_tile_service_visibility(sid, vis)
            if vis == "groups":
                for gid in chosen:
                    self._client.share_tile_service_with_group(sid, gid)
        elif kind == "tiles3d":
            sid = payload.get("id")
            self._client.set_tiles3d_visibility(sid, vis)
            if vis == "groups":
                for gid in chosen:
                    self._client.share_tiles3d_with_group(sid, gid)

    def _on_bulk_shared(self, results, total):
        """Report the bulk-share outcome — a partial failure is NEVER swallowed.

        ``results`` is the per-item ``(title, ok, error)`` list. On any failed
        item, a message box lists WHICH items failed and why; otherwise a
        single success toast.
        """
        failed = [(title, err) for (title, ok, err) in results if not ok]
        if failed:
            lines = "\n".join("• {}: {}".format(t, e or "failed")
                              for (t, e) in failed)
            self._warn(
                "geoi",
                "Sharing updated for {} of {} items. {} failed:\n{}".format(
                    total - len(failed), total, len(failed), lines))
        else:
            self._info("Sharing updated for {} items.".format(total))

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

    def _tile_detail(self, payload):
        """Fetch a tile service's detail ONCE: its per-format URLs AND its WGS84
        bounds, in a single round-trip (WS4 — the add path frames the layer to
        the service extent without a second ``tile_service`` fetch).

        Returns ``({xyz, wmts, pmtiles}, bounds, None)`` on success or
        ``({}, None, error)``. ``bounds`` is whatever the detail carries (a
        4-elem WGS84 list or a min/max dict) or ``None`` when absent.
        """
        sid = payload.get("id")
        if sid in (None, ""):
            return {}, None, "This tile service has no id."
        try:
            detail = self._client.tile_service(sid)
        except GeoiError as exc:
            return {}, None, friendly_error(exc)
        bounds = detail.get("bounds") if isinstance(detail, dict) else None
        return self._client.tile_format_urls(detail), bounds, None

    def _tile_format_urls(self, payload):
        """Fetch a tile service's per-format URLs (absolute + tokenized).

        Returns ``({xyz, wmts, pmtiles}, None)`` on success or ``({}, error)``
        so the caller can show a friendly message.
        """
        urls, _bounds, err = self._tile_detail(payload)
        return urls, err

    def _set_layer_extent_from_bounds(self, layer, bounds):
        """Frame ``layer`` to its service's WGS84 ``bounds`` (WS4).

        Defensively accepts BOTH a 4-elem WGS84 list ``[w, s, e, n]`` AND a dict
        (``{minx, miny, maxx, maxy}`` or ``{west, south, east, north}``),
        reprojects EPSG:4326 -> the layer's own CRS, and calls
        ``layer.setExtent`` on the result. Null / absent / unparseable bounds
        are skipped so the layer keeps QGIS's default (world) extent — no
        regression when a service carries no bounds.
        """
        coords = _parse_bounds(bounds)
        if coords is None:
            return
        try:
            from qgis.core import (
                QgsCoordinateReferenceSystem,
                QgsCoordinateTransform,
                QgsProject,
                QgsRectangle,
            )

            west, south, east, north = coords
            rect = QgsRectangle(west, south, east, north)
            xform = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                layer.crs(), QgsProject.instance())
            layer.setExtent(xform.transformBoundingBox(rect))
        # security review: zooming/framing the new layer must never break the add
        except Exception:  # nosec B110
            pass

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
                # security review: clipboard copy is best-effort; the
                # visibility update itself already succeeded
                except Exception:  # nosec B110
                    pass
            self._info("Visibility set to {}.".format(vis))

        self._run_action("updating tile sharing", work, on_ok)

    def share_tiles3d_service(self, payload):
        """Set a 3D Tiles service's visibility and reconcile its group shares.

        Mirrors ``share_item`` for feature services — 3D-Tiles Services support
        REAL group shares (``share_tiles3d_with_group`` /
        ``unshare_tiles3d_group``), so ticking / clearing groups adds and
        removes exactly those shares, gated on the ``groups`` visibility.
        """
        if not self._signed_in():
            return
        sid = payload.get("id")
        if sid in (None, ""):
            return self._warn("geoi", "This 3D Tiles service has no id.")
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
            self._client.set_tiles3d_visibility(sid, vis)
            if vis == "groups":
                for gid in chosen - before:
                    self._client.share_tiles3d_with_group(sid, gid)
                for gid in before - chosen:
                    self._client.unshare_tiles3d_group(sid, gid)
            return True

        self._run_action("updating sharing", work,
                         lambda _p: self._info("Sharing updated."))

    def copy_project_url(self, payload):
        """Copy a web map's (project) deep-link URL — ``<base>/?project=<id>`` —
        used by the Discover menu's generic "Copy URL"."""
        pid = (payload or {}).get("id")
        if pid in (None, ""):
            return
        url = "{base}/?project={pid}".format(
            base=self._client.base_url, pid=_quote(str(pid)))
        try:
            QApplication.clipboard().setText(url)
            self._info("Web map URL copied:\n" + url)
        except Exception:  # noqa: BLE001
            self._info(url)

    def copy_public_url(self, kind, payload):
        """Copy a discovered item's URL, dispatched per kind (the Discover
        menu's generic "Copy URL")."""
        if kind == "service":
            self.copy_service_url(payload)
        elif kind == "tile":
            self.copy_tile_url(payload, "xyz")
        elif kind == "tiles3d":
            self.copy_tiles3d_url(payload)
        elif kind == "project":
            self.copy_project_url(payload)

    def open_public_in_web_app(self, kind, payload):
        """Open a discovered item in the geoi web app, dispatched per kind (the
        Discover menu's generic "Open in geoi")."""
        if kind == "tile":
            self.open_tile_in_web_app(payload)
        elif kind == "tiles3d":
            self.open_tiles3d_preview_in_web_app(payload)
        else:
            # Projects + feature services open the geoi web app (mirrors the
            # owned-project "Open in geoi" path); never re-dispatch to add-to-map
            # here — that is the separate "Add to map" action.
            self.open_web_app()

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
        urls, bounds, err = self._tile_detail(payload)
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
            # Frame the layer to the service's own WGS84 extent (WS4) before we
            # zoom, so the canvas jumps to the data instead of the whole world.
            self._set_layer_extent_from_bounds(layer, bounds)
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
        urls, bounds, err = self._tile_detail(payload)
        if err:
            return self._warn("geoi", "Could not read the tile service:\n" + err)
        wmts = urls.get("wmts")
        if not wmts:
            return self._warn("geoi", "This tile service has no WMTS URL.")
        name = _title(payload)
        source = self._wmts_uri(wmts, payload)
        layer = QgsRasterLayer(source, name, "wms")
        if layer.isValid():
            self._set_layer_extent_from_bounds(layer, bounds)
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
        elif kind == "tiles3d":
            sid = payload.get("id")
            self._run_action(
                "renaming 3D Tiles service",
                lambda: self._client.rename_tiles3d(sid, new_name),
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
        elif kind == "tiles3d":
            sid = payload.get("id")
            self._run_action(
                "moving 3D Tiles service",
                lambda: self._client.move_tiles3d(sid, folder_id),
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
        # Human wording for kinds whose internal name reads poorly in a
        # sentence; the established kinds keep their exact existing text.
        kind_label = {"tiles3d": "3D Tiles service"}.get(kind, kind)
        if QMessageBox.question(
            self.iface.mainWindow(), "Delete {}".format(kind_label),
            "Delete {} '{}'? This cannot be undone.".format(kind_label, label),
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
        elif kind == "tiles3d":
            self._run_action(
                "deleting 3D Tiles service",
                lambda: self._client.tiles3d_delete(payload.get("id")),
                lambda _p: self._info(
                    "3D Tiles service '{}' deleted.".format(label)),
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
            # A None accumulator seeds on the first non-empty extent, so no
            # deprecated `setMinimal()` sentinel rect is needed (the copy
            # constructor + combineExtentWith are stable QGIS 3.22-4.99).
            box = None
            for vlayer in layers:
                try:
                    vlayer.updateExtents()
                # security review: updateExtents() is absent on scene/raster layers
                except Exception:  # nosec B110
                    pass
                extent = vlayer.extent()
                if extent.isEmpty():
                    continue
                xform = QgsCoordinateTransform(vlayer.crs(), dest_crs, project)
                ext = xform.transformBoundingBox(extent)
                if box is None:
                    box = QgsRectangle(ext)
                else:
                    box.combineExtentWith(ext)
            if box is not None and not box.isEmpty():
                box.scale(1.05)
                canvas.setExtent(box)
            canvas.refresh()
        except Exception:  # noqa: BLE001 - framing must never break adding
            try:
                self.iface.mapCanvas().refresh()
            # security review: canvas refresh after a framing failure is cosmetic
            except Exception:  # nosec B110
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

    # ----------------------------------------------------- publish 3D tiles
    _TILES3D_FILE_FILTER = "3D data (*.las *.laz *.ply *.zip)"

    def publish_tiles3d(self):
        """Publish 3D data as a geoi 3D Tiles service.

        MULTI-select entry point. A prepared tileset ZIP keeps the existing
        upload lane (exactly one ZIP at a time; the plugin does NOT prepare
        those tiles). LAS / LAZ / PLY point clouds take the point-cloud lane:
        ONE service — the first file creates it, every further file is
        appended via ``POST /tiles3d/services/<id>/add`` — with a per-file
        reproject-to-WGS84 / keep-original choice made up front.
        """
        if not self._signed_in():
            return self._warn("geoi", "Please sign in first.")
        from qgis.PyQt.QtWidgets import QFileDialog

        paths, _ = QFileDialog.getOpenFileNames(
            self.iface.mainWindow(),
            "Choose 3D data to publish", "",
            self._TILES3D_FILE_FILTER,
        )
        if not paths:
            return
        zips = [p for p in paths if _is_zip_path(p)]
        clouds = [p for p in paths if not _is_zip_path(p)]
        if zips and clouds:
            return self._warn(
                "geoi",
                "Pick either point-cloud files (.las / .laz / .ply) or ONE "
                "prepared tileset ZIP — not both in one publish.",
            )
        if zips:
            if len(zips) > 1:
                return self._warn(
                    "geoi", "Publish prepared tileset ZIPs one at a time.")
            return self._publish_tiles3d_zip(zips[0])
        return self._publish_point_clouds(clouds)

    def _publish_tiles3d_zip(self, zip_path):
        """The prepared-ZIP lane (unchanged): validate client-side, name it,
        POST to /tiles3d/create — same endpoint / storage / quota as the app.
        """
        from . import tiles3d

        # Fail FAST on an invalid tileset BEFORE any upload (actionable text).
        try:
            tiles3d.validate_tileset_zip(zip_path)
        except tiles3d.Tiles3dError as exc:
            return self._warn("geoi — not a 3D Tiles ZIP", str(exc))

        title, ok = QInputDialog.getText(
            self.iface.mainWindow(), "Publish 3D Tiles", "Tileset name",
            text=tiles3d.default_title(zip_path),
        )
        if not ok:
            return
        title = (title or "").strip() or tiles3d.default_title(zip_path)

        def done(ok, payload):
            if ok:
                url = payload.get("tilesetUrl", "")
                name = payload.get("title", title)
                self._log_msg("Published 3D tiles '{}' -> {}".format(name, url))
                # Put the tileset URL on the clipboard so it can be pasted
                # straight into Cesium / ArcGIS.
                try:
                    QApplication.clipboard().setText(url)
                # security review: clipboard copy is best-effort
                except Exception:  # nosec B110
                    pass
                QMessageBox.information(
                    self.iface.mainWindow(),
                    "geoi — 3D tiles published",
                    "3D Tiles service '{}' was published.\n\nIts tileset URL "
                    "(copied to the clipboard) — paste it into Cesium or any "
                    "3D Tiles viewer:\n\n{}".format(name, url),
                )
                self.refresh()
            else:
                self._warn("geoi 3D tiles publish failed", str(payload))

        self._info("Uploading the 3D Tiles tileset…")
        task = Tiles3dPublishTask(self._client, zip_path, title, done)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    # ------------------------------------------- point clouds -> 3D Tiles
    def _publish_point_clouds(self, paths):
        """The point-cloud lane: one CRS-choice dialog for ALL selected files,
        then a sequential create→add chain into ONE 3D Tiles service."""
        # Lazy: the dialog is new in this wave; resolving it at call time
        # keeps the module importable against a stubbed gui package in tests.
        from .gui.dialogs import Tiles3dCrsChoiceDialog

        infos = []
        for path in paths:
            epsg = _las_header_epsg(path)
            infos.append({
                "path": path,
                "name": os.path.basename(path),
                "crs": "EPSG:{}".format(epsg) if epsg else None,
            })
        first_stem = os.path.splitext(os.path.basename(paths[0]))[0] or "3D tiles"
        dlg = Tiles3dCrsChoiceDialog(
            infos, default_title=first_stem, parent=self.iface.mainWindow())
        if not dlg.exec():
            return
        title = dlg.title() or first_stem
        crs_by_path = {i["path"]: i["crs"] for i in infos}
        jobs = []
        for choice in dlg.choices():
            path = choice.get("path")
            # 'reproject' | 'local' — the values tiles3d.publish_point_cloud
            # takes for its `placement` argument.
            placement = choice.get("placement") or "reproject"
            # A QGIS-backed WGS84 reprojector only when the source CRS is
            # KNOWN (LAS/LAZ header peek); None -> the encoder's built-in
            # placement path resolves the CRS itself at publish time.
            reproject = None
            if placement == "reproject":
                reproject = _make_reproject_fn(crs_by_path.get(path))
            jobs.append({"path": path, "placement": placement,
                         "reproject": reproject})
        if not jobs:
            return
        self._info("Publishing {} point cloud file(s) as 3D Tiles…".format(
            len(jobs)))
        self._start_point_cloud_chain(title, jobs)

    def _start_point_cloud_chain(self, title, jobs):
        """First file CREATES the service; each further file is ADDED to it.

        Sequential — each task's done-callback launches the next, so uploads
        never overlap. A failed CREATE aborts everything; a failed ADD is
        best-effort: the chain continues and the final summary names every
        file that was not added. Progress rides the message bar like the
        other publish flows.
        """
        # Lazy import — Tiles3dPointCloudPublishTask lands in tasks.py in the
        # sibling wave of this feature; resolving it at call time keeps this
        # module importable against a stubbed/older tasks module.
        from .tasks import ActionTask, Tiles3dPointCloudPublishTask

        first, rest = jobs[0], jobs[1:]
        failures = []

        def finish(payload):
            payload = payload or {}
            url = payload.get("tilesetUrl", "")
            name = payload.get("title") or title
            self._log_msg("Published 3D tiles '{}' -> {}".format(name, url))
            try:
                QApplication.clipboard().setText(url)
            # security review: clipboard copy is best-effort
            except Exception:  # nosec B110
                pass
            summary = "3D Tiles service '{}' was published ({} of {} file(s)).".format(
                name, len(jobs) - len(failures), len(jobs))
            if url:
                summary += "\n\nTileset URL (copied to the clipboard):\n" + url
            if failures:
                summary += "\n\nNot added:\n" + "\n".join(failures)
            QMessageBox.information(
                self.iface.mainWindow(), "geoi — 3D tiles published", summary)
            self.refresh()

        def run_add(idx, service_id, create_payload):
            if idx >= len(rest):
                return finish(create_payload)
            job = rest[idx]
            fname = os.path.basename(job["path"])
            self._info("Adding 3D dataset {} of {}: {}…".format(
                idx + 2, len(jobs), fname))

            def work():
                return self._add_point_cloud_to_service(
                    service_id, job["path"], job["placement"],
                    job["reproject"])

            def done(ok, payload):
                if not ok:
                    failures.append("{} — {}".format(fname, payload))
                run_add(idx + 1, service_id, create_payload)

            task = ActionTask("adding 3D dataset", work, done)
            task.taskCompleted.connect(lambda: self._done(task))
            task.taskTerminated.connect(lambda: self._done(task))
            self._run(task)

        def created(ok, payload):
            if not ok:
                # The CREATE failing aborts the whole chain — nothing exists
                # to add the remaining files to.
                return self._warn("geoi 3D tiles publish failed", str(payload))
            service_id = (payload or {}).get("id")
            if service_id in (None, "") and isinstance(
                    (payload or {}).get("service"), dict):
                service_id = payload["service"].get("id")
            if rest and service_id in (None, ""):
                failures.extend(
                    os.path.basename(j["path"]) + " — the created service "
                    "returned no id" for j in rest)
                return finish(payload)
            run_add(0, service_id, payload)

        self._info("Publishing 3D dataset 1 of {}: {}…".format(
            len(jobs), os.path.basename(first["path"])))
        task = Tiles3dPointCloudPublishTask(
            self._client, first["path"], title, first["placement"],
            first["reproject"], created)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    def _add_point_cloud_to_service(self, service_id, path, placement,
                                    reproject_fn):
        """Encode + append ONE further point-cloud file to an EXISTING 3D
        Tiles service (runs on the worker thread — blocking is fine here).

        The point-cloud encoder exposes ONE upload entry point —
        ``tiles3d.publish_point_cloud`` — which POSTs the encoded tileset ZIP
        through ``client.tiles3d_create`` (the only create call the client
        offers). To land the SAME encoded ZIP on the
        ``/tiles3d/services/<id>/add`` endpoint instead, hand it a thin
        routing proxy whose ``tiles3d_create`` forwards to the real client's
        ``tiles3d_add(service_id, …)``; every other attribute delegates
        untouched (see ``_Tiles3dAddRouter``).
        """
        from . import tiles3d

        stem = os.path.splitext(os.path.basename(path))[0] or "dataset"
        proxy = _Tiles3dAddRouter(self._client, service_id)
        return tiles3d.publish_point_cloud(
            proxy, path, stem, placement, reproject_fn,
            progress=None, is_cancelled=None)

    # ----------------------------------------------------- 3D Tiles services
    def _tiles3d_tileset_share_url(self, payload):
        """The absolute (token-appended for a non-public service)
        tileset.json URL for a 3D Tiles service — ``(url, None)`` or
        ``("", error)``.

        Mirrors ``_tile_format_urls``: fetch the detail, then absolutize +
        tokenize via ``tile_url`` (``?token=<shareToken>`` ONLY when the
        service is not public) — exactly like the raster add path.
        """
        sid = (payload or {}).get("id")
        if sid in (None, ""):
            return "", "This 3D Tiles service has no id."
        try:
            detail = self._client.tiles3d_get(sid)
        except GeoiError as exc:
            return "", tiles3d_friendly_error(exc)
        urls = detail.get("urls") if isinstance(detail.get("urls"), dict) else {}
        url = self._client.tile_url(
            urls.get("tileset"),
            share_token=detail.get("shareToken"),
            visibility=detail.get("visibility") or payload.get("visibility"),
        )
        if not url:
            return "", "This 3D Tiles service has no tileset URL."
        return url, None

    def add_tiles3d_layer(self, payload):
        """Add a 3D Tiles service to the map as a native tiled-scene layer.

        ``QgsTiledSceneLayer(uri, title, 'cesiumtiles')`` — QGIS 3.34+ only
        (the class + the cesiumtiles provider shipped in 3.34); an older QGIS
        gets a message-bar pointer to the deck.gl preview instead. The
        datasource is the ``url=<tileset.json>`` provider form
        (``tiles3d_layer_uri``) with a plain-URL fallback if a build rejects
        it. Add-to-project + zoom mirror the other add flows.
        """
        if not _tiled_scene_support():
            return self._warn_bar(_TILES3D_NEEDS_QGIS_MSG)
        url, err = self._tiles3d_tileset_share_url(payload)
        if err:
            return self._warn(
                "geoi", "Could not read the 3D Tiles service:\n" + err)
        # The cesiumtiles provider renders only MESH tilesets — a point-cloud
        # tileset (geoi's encoder emits POINTS-mode .glb) loads "valid" but
        # shows NOTHING. Sniff the first content tile and refuse it up front
        # with an actionable pointer, rather than a false "Added" success. The
        # WHOLE sniff fails OPEN: any error here proceeds to the normal add.
        try:
            from . import tiles3d

            tileset = self._client.tiles3d_tileset_json(url, timeout=6)
            if tileset:
                def fetch_glb(content_uri):
                    return self._client.fetch_bytes(
                        _tileset_content_url(url, content_uri),
                        timeout=6, max_bytes=262144)

                if tiles3d.tileset_is_point_cloud(tileset, fetch_glb):
                    return self._warn_bar(_TILES3D_POINT_CLOUD_MSG)
        # security review: the point-cloud sniff must never block adding a mesh tileset
        except Exception:  # nosec B110
            pass
        name = _title(payload)
        try:
            from qgis.core import QgsTiledSceneLayer

            layer = QgsTiledSceneLayer(
                tiles3d_layer_uri(url), name, "cesiumtiles")
            if not layer.isValid():
                # Some builds accept the bare URL as the datasource.
                layer = QgsTiledSceneLayer(url, name, "cesiumtiles")
        except Exception:  # noqa: BLE001 - class missing despite the gate
            return self._warn_bar(_TILES3D_NEEDS_QGIS_MSG)
        if layer.isValid():
            # Scene layers sit with the rasters — under the vectors.
            self._add_layer_to_toc(layer, top=False)
            self._zoom_to_layers([layer])
            self._info("Added 3D Tiles service '{}' to the map.".format(name))
        else:
            self._warn(
                "geoi",
                "QGIS could not load the 3D Tiles layer. Copy the tileset "
                "URL and add it via Layer → Add Layer → Add Scene Layer "
                "instead.",
            )

    def preview3d_deeplink_url(self, payload, engine="deck"):
        """Build the web-app deep link that OPENS a 3D Tiles service in one of
        the geoi in-app 3D previews (deck.gl or Cesium).

        ``<base_url>/?preview3d=<id>``; for a NON-public service the stable
        ``shareToken`` is appended as ``&ptoken=<token>`` so it opens without
        a sign-in. ``engine`` picks the viewer (``deck`` default, or
        ``cesium``); it is appended as ``&engine=<engine>`` only when it is
        NOT the default so existing deck-only links stay byte-identical (the
        web receiver defaults to deck when the param is absent). Mirrors
        ``tile_deeplink_url`` (#8), including how ``base_url`` is resolved.
        Returns ``(url, None)`` or ``("", error)``.
        """
        sid = (payload or {}).get("id")
        if sid in (None, ""):
            return "", "This 3D Tiles service has no id."
        try:
            detail = self._client.tiles3d_get(sid)
        except GeoiError as exc:
            return "", tiles3d_friendly_error(exc)
        # Prefer the freshly-read detail's id; fall back to the summary id.
        tid = detail.get("id", sid)
        url = "{base}/?preview3d={id}".format(
            base=self._client.base_url, id=_quote(str(tid)))
        visibility = detail.get("visibility") or payload.get("visibility")
        token = detail.get("shareToken")
        if token and visibility != "public":
            url += "&ptoken=" + _quote(str(token))
        eng = "cesium" if str(engine).lower() == "cesium" else "deck"
        if eng != "deck":
            url += "&engine=" + _quote(eng)
        return url, None

    def open_tiles3d_preview_in_web_app(self, payload, engine="deck"):
        """Open a geoi 3D preview (deck.gl or Cesium) for a 3D Tiles service
        in the system browser — mirrors ``open_tile_in_web_app``."""
        url, err = self.preview3d_deeplink_url(payload, engine=engine)
        if err:
            return self._warn(
                "geoi", "Could not read the 3D Tiles service:\n" + err)
        eng_label = "Cesium" if str(engine).lower() == "cesium" else "deck.gl"
        try:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(url))
            self._info(
                "Opening the {eng} preview in the geoi web app…".format(
                    eng=eng_label))
        except Exception:  # noqa: BLE001
            self._warn("geoi", "Could not open the browser.")

    def copy_tiles3d_url(self, payload):
        """Copy a 3D Tiles service's (tokenized) tileset.json URL — mirrors
        ``copy_tile_url``; paste into Cesium, ArcGIS or QGIS."""
        url, err = self._tiles3d_tileset_share_url(payload)
        if err:
            return self._warn(
                "geoi", "Could not read the 3D Tiles service:\n" + err)
        try:
            QApplication.clipboard().setText(url)
            self._info("Tileset URL copied:\n" + url)
        except Exception:  # noqa: BLE001
            self._info(url)

    def _warn_bar(self, text):
        """A non-blocking message-bar warning (falls back to ``_info``)."""
        try:
            self.iface.messageBar().pushWarning("geoi", text)
        except Exception:  # noqa: BLE001 - messaging must never break a flow
            self._info(text)


class _Tiles3dAddRouter:
    """A GeoiClient proxy that reroutes ``tiles3d_create`` to ``tiles3d_add``.

    ``tiles3d.publish_point_cloud`` always uploads through the client's
    ``tiles3d_create``; wrapping the client in this router makes the SAME
    encode+upload path APPEND to an existing service (``POST
    /tiles3d/services/<id>/add``) instead of creating a new one. Every other
    attribute (``tiles3d_tileset_url``, ``base_url``, …) delegates to the
    real client untouched.
    """

    def __init__(self, client, service_id):
        self._client = client
        self._service_id = service_id

    def tiles3d_create(self, zip_path_or_bytes, title="", filename=None,
                       bounds=None):
        # Same bytes-or-path + bounds contract as GeoiClient.tiles3d_create
        # (the encoder feature-detects the `bounds` kwarg on THIS method and
        # rides the cloud's WGS84 bounds through it — the server unions them
        # into the service row). ``title`` is meaningless on an add (the
        # service keeps its own) — dropped.
        if isinstance(zip_path_or_bytes, (bytes, bytearray)):
            data = bytes(zip_path_or_bytes)
            name = filename or "tileset.zip"
        else:
            with open(zip_path_or_bytes, "rb") as handle:
                data = handle.read()
            name = (filename or os.path.basename(zip_path_or_bytes)
                    or "tileset.zip")
        return self._client.tiles3d_add(
            self._service_id, data, filename=name, bounds=bounds)

    def __getattr__(self, name):
        return getattr(self._client, name)


# QgsTiledSceneLayer + the cesiumtiles provider shipped in QGIS 3.34.
_TILES3D_MIN_QGIS = 33400
_TILES3D_NEEDS_QGIS_MSG = (
    "Adding 3D Tiles to the map needs QGIS 3.34+ — use 'Open deck.gl "
    "preview' instead."
)
_TILES3D_POINT_CLOUD_MSG = (
    "QGIS can't render point-cloud 3D Tiles (only mesh tilesets). Use "
    "'Open deck.gl preview' or 'Open Cesium preview in web app' instead."
)


def _tiled_scene_support():
    """True when this QGIS can host a 3D Tiles layer: 3.34+ AND the
    ``QgsTiledSceneLayer`` class importable. Guarded so an old (or stubbed)
    QGIS simply reports False — the caller shows the deck.gl pointer."""
    try:
        from qgis.core import Qgis

        if int(getattr(Qgis, "QGIS_VERSION_INT", 0)) < _TILES3D_MIN_QGIS:
            return False
        from qgis.core import QgsTiledSceneLayer  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - no/partial QGIS -> no native 3D Tiles
        return False


def tiles3d_layer_uri(url):
    """The ``QgsTiledSceneLayer`` datasource string for the ``cesiumtiles``
    provider: the ``url=<tileset.json URL>`` key=value form QGIS's own Scene
    → Add Scene Layer dialog produces (the provider's decodeUri reads the
    ``url`` part; the value itself is not percent-encoded). Pure/stdlib so it
    is unit-testable off QGIS; the add path keeps a plain-URL fallback for a
    build that rejects this form."""
    return "url=" + (url or "")


def _tileset_content_url(tileset_url, content_uri):
    """Resolve a tileset content uri (typically RELATIVE to the tileset.json)
    to an ABSOLUTE url, carrying the tileset URL's query (the share token)
    onto it so a token-gated tileset's GLB is fetchable too.

    Pure ``urllib.parse`` split/join — never string concatenation that could
    double up ``?`` / ``&``. Pure/stdlib so it is unit-testable off QGIS."""
    base = urllib.parse.urlsplit(tileset_url or "")
    # urljoin against the tileset.json's own path (query stripped) resolves a
    # relative "0.glb" to ".../services/5/0.glb"; an absolute content uri is
    # passed straight through.
    parent = urllib.parse.urlunsplit(
        (base.scheme, base.netloc, base.path, "", ""))
    parts = urllib.parse.urlsplit(
        urllib.parse.urljoin(parent, content_uri or ""))
    # Merge the tileset URL's share-token query with any query the content uri
    # itself carried — join with "&", never a blind concatenation.
    query = "&".join(q for q in (parts.query, base.query) if q)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, ""))


def _is_zip_path(path):
    """True for a ``.zip`` path (case-insensitive) — the prepared-ZIP lane."""
    return os.path.splitext(path or "")[1].lower() == ".zip"


def _make_reproject_fn(src_crs):
    """A ``(x, y) -> (lat, lon)`` WGS84 reprojector for a KNOWN source CRS.

    Built on QGIS's own transform stack when it is available and ``src_crs``
    (e.g. ``"EPSG:25832"``) resolves to a valid CRS; otherwise ``None`` so
    the point-cloud encoder falls back to its built-in placement path.
    """
    if not src_crs:
        return None
    try:
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsPointXY,
            QgsProject,
        )

        src = QgsCoordinateReferenceSystem(str(src_crs))
        if hasattr(src, "isValid") and not src.isValid():
            return None
        xform = QgsCoordinateTransform(
            src, QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance())
    except Exception:  # noqa: BLE001 - no QGIS / bad CRS -> encoder default
        return None

    def reproject(x, y):
        pt = xform.transform(QgsPointXY(x, y))
        return (pt.y(), pt.x())  # (lat, lon)

    return reproject


def _las_header_epsg(path):
    """Best-effort EPSG code from a LAS/LAZ header — ``None`` when unknown.

    LAS and LAZ share the same UNCOMPRESSED public header + VLRs (LASzip only
    compresses the point records), so a tiny stdlib peek can read the CRS
    with no point-cloud dependency: walk the VLRs for the
    ``LASF_Projection`` GeoTIFF key directory (record 34735) and return the
    ProjectedCSType (3072) or GeographicType (2048) GeoKey, or fish the EPSG
    id out of the WKT VLR (record 2112). Every read is bounded; ANY oddity
    returns ``None`` — the CRS dialog then says "detected at publish time"
    and the encoder resolves it itself.
    """
    import re
    import struct

    try:
        ext = os.path.splitext(path or "")[1].lower()
        if ext not in (".las", ".laz"):
            return None
        with open(path, "rb") as fh:
            header = fh.read(375)
            if len(header) < 227 or header[:4] != b"LASF":
                return None
            header_size = struct.unpack_from("<H", header, 94)[0]
            vlr_count = struct.unpack_from("<I", header, 100)[0]
            if header_size < 227 or vlr_count > 64:
                return None
            fh.seek(header_size)
            for _ in range(vlr_count):
                vlr = fh.read(54)
                if len(vlr) < 54:
                    return None
                user_id = vlr[2:18].split(b"\x00", 1)[0]
                record_id = struct.unpack_from("<H", vlr, 18)[0]
                length = struct.unpack_from("<H", vlr, 20)[0]
                payload = fh.read(length)
                if len(payload) < length or user_id != b"LASF_Projection":
                    continue
                if record_id == 34735 and length >= 8:
                    n_keys = struct.unpack_from("<H", payload, 6)[0]
                    geographic = None
                    for i in range(min(n_keys, (length - 8) // 8)):
                        key_id, location, _cnt, value = struct.unpack_from(
                            "<4H", payload, 8 + i * 8)
                        if location != 0:
                            continue
                        if key_id == 3072 and 0 < value < 65535:
                            return int(value)  # projected CRS wins
                        if key_id == 2048 and 0 < value < 65535:
                            geographic = int(value)
                    if geographic:
                        return geographic
                elif record_id == 2112:
                    text = payload.decode("ascii", "replace")
                    found = re.findall(
                        r'(?:AUTHORITY|ID)\[\s*"EPSG"\s*,\s*"?(\d{4,6})"?',
                        text)
                    if found:
                        return int(found[-1])
    except Exception:  # noqa: BLE001 - a peek must never break the dialog
        return None
    return None


def _parse_bounds(bounds):
    """Normalise a WGS84 bounds value to ``(west, south, east, north)`` floats,
    or ``None`` when it is absent / unparseable (WS4).

    Accepts BOTH a 4-elem list/tuple ``[w, s, e, n]`` AND a dict keyed by the
    common min/max or compass conventions (``minx/miny/maxx/maxy`` or
    ``west/south/east/north``, plus a few aliases). Pure / stdlib so it is
    unit-testable off QGIS.
    """
    try:
        if isinstance(bounds, (list, tuple)):
            if len(bounds) < 4:
                return None
            west, south, east, north = (
                float(bounds[0]), float(bounds[1]),
                float(bounds[2]), float(bounds[3]))
        elif isinstance(bounds, dict):
            def pick(*keys):
                for key in keys:
                    if bounds.get(key) is not None:
                        return float(bounds[key])
                raise KeyError(keys)

            west = pick("minx", "west", "xmin", "minX", "minLon", "w")
            south = pick("miny", "south", "ymin", "minY", "minLat", "s")
            east = pick("maxx", "east", "xmax", "maxX", "maxLon", "e")
            north = pick("maxy", "north", "ymax", "maxY", "maxLat", "n")
        else:
            return None
    except (TypeError, ValueError, KeyError):
        return None
    return (west, south, east, north)


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
