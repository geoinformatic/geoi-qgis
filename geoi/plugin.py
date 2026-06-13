"""geoi QGIS plugin — controller that wires the panel, client and tasks."""

import json
import os

from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QInputDialog, QMessageBox

from . import convert, settings
from .auth import SessionStore
from .geoi_client import GeoiClient, GeoiError, epsg_from_spatial_reference
from .gui.browser_panel import GeoiPanel
from .gui.dialogs import (
    MoveToFolderDialog,
    PublishDialog,
    SaveProjectDialog,
    SettingsDialog,
    ShareDialog,
)
from .tasks import ActionTask, CatalogTask, PublishTask, SaveProjectTask, SignInTask

_ICON = os.path.join(os.path.dirname(__file__), "icon.svg")


class GeoiPlugin:
    def __init__(self, iface):
        self.iface = iface
        self._store = SessionStore()
        self._client = GeoiClient(base_url=settings.base_url(), log=self._log_msg)
        self._panel = None
        self._action = None
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

        self._restore_session()

    def unload(self):
        if self._action is not None:
            self.iface.removeToolBarIcon(self._action)
            self.iface.removePluginWebMenu("geoi", self._action)
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
        token = self._store.load_token()
        if token:
            self._client.set_token(token)
            self._panel.set_signed_in(settings.session_user())
            self.refresh()

    def open_settings(self):
        dlg = SettingsDialog(self.iface.mainWindow())
        if dlg.exec():
            self._client.base_url = GeoiClient(base_url=settings.base_url()).base_url

    # ----------------------------------------------------------------- auth
    def sign_in(self):
        self._panel.set_busy("Opening your browser to sign in with Google…")

        def done(ok, payload):
            if ok:
                self._finish_sign_in(payload.get("user", {}))
            else:
                self._panel.set_busy("")
                self._offer_paste(payload)

        task = SignInTask(self._client, done)
        task.taskCompleted.connect(lambda: self._done(task))
        task.taskTerminated.connect(lambda: self._done(task))
        self._run(task)

    def _finish_sign_in(self, user):
        self._store.save_token(self._client.token)
        settings.set_session_user(user)
        authcfg = self._store.ensure_header_authcfg(
            self._client.token, settings.header_authcfg()
        )
        settings.set_header_authcfg(authcfg)
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

    # --------------------------------------------------------- add service
    def add_service(self, service):
        name = service.get("name")
        if not name:
            return
        authcfg = "" if service.get("visibility") == "public" else settings.header_authcfg()
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
                QgsProject.instance().addMapLayer(vlayer)
                added.append(vlayer)
        if added:
            self._zoom_to_layers(added)
            self._info("Added {} layer(s) from '{}'.".format(len(added), name))
        else:
            self._warn(
                "geoi",
                "QGIS could not load the layers. For a private service, make sure "
                "you are signed in (the bearer token is attached automatically).",
            )

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


def _project_name():
    title = QgsProject.instance().title() or QgsProject.instance().baseName()
    return title or "QGIS project"


def _title(entry):
    return (entry or {}).get("title") or (entry or {}).get("name") \
        or str((entry or {}).get("id", ""))
