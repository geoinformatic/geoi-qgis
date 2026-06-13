"""The dockable geoi panel: sign in, browse your content in folders, act on it."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from qgis.gui import QgsDockWidget as _Dock
except ImportError:  # very old QGIS
    from qgis.PyQt.QtWidgets import QDockWidget as _Dock

from .. import content_tree

# Custom item-data roles. Plain ints (Qt.UserRole == 256) so the module
# imports cleanly on both Qt5 and Qt6 — PyQt6 scopes its enums and has no
# unscoped ``Qt.UserRole``.
ROLE_KIND = 256
ROLE_PAYLOAD = 257
ROLE_FOLDER_ID = 258
ROLE_NAME = 259  # the committed display name, to detect/revert inline renames


class _ContentTree(QTreeWidget):
    """A tree that turns an internal drag-drop into a server-side folder move.

    Qt's own InternalMove would reorder the widget locally and drift from the
    server; instead we intercept the drop, work out the destination folder
    and ask the controller to perform the move, then the panel refreshes from
    the hub so the view always matches reality.
    """

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event):  # noqa: N802 - Qt override
        point = (event.position().toPoint()
                 if hasattr(event, "position") else event.pos())
        target = self.itemAt(point)
        dragged = self.currentItem()
        event.ignore()  # never let Qt move the rows itself
        if dragged is None:
            return
        kind = dragged.data(0, ROLE_KIND)
        if kind not in ("service", "project", "folder"):
            return
        self._panel._move_to(dragged, _folder_target(target))


def _folder_target(item):
    """The destination folder id for a drop on ``item`` ("" means the root)."""
    while item is not None:
        if item.data(0, ROLE_KIND) == "folder":
            return item.data(0, ROLE_FOLDER_ID) or ""
        item = item.parent()
    return ""


class GeoiPanel(_Dock):
    def __init__(self, controller, parent=None):
        super().__init__("geoi", parent)
        self.setObjectName("GeoiPanel")
        self._c = controller
        self._folders = []

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # --- account row ---
        acct = QHBoxLayout()
        self._account = QLabel("Not signed in")
        self._account.setWordWrap(True)
        self._sign_btn = QPushButton("Sign in with Google")
        self._sign_btn.clicked.connect(self._on_sign_clicked)
        self._settings_btn = QToolButton()
        self._settings_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self._settings_btn.setToolTip("Server settings")
        self._settings_btn.clicked.connect(self._c.open_settings)
        acct.addWidget(self._account, 1)
        acct.addWidget(self._sign_btn)
        acct.addWidget(self._settings_btn)
        layout.addLayout(acct)

        # --- content tree ---
        self._tree = _ContentTree(self)
        self._tree.setHeaderLabels(["Name", "Visibility"])
        self._tree.setColumnWidth(0, 220)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Rename "by just clicking": F2, or a click on an already-selected row,
        # opens the inline editor (like a file manager). Double-click still
        # adds a service to the map.
        self._tree.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_double_click)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.itemSelectionChanged.connect(self._update_buttons)
        self._loading = False  # guards itemChanged during programmatic fills
        # Visual polish — palette-aware (no hardcoded colours), so it follows
        # QGIS light/dark themes.
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setIndentation(14)
        self._tree.setAnimated(True)
        self._tree.setExpandsOnDoubleClick(False)  # double-click adds, not expand
        self._tree.header().setStretchLastSection(True)
        layout.addWidget(self._tree, 1)

        sp = QStyle.StandardPixmap
        # --- action row ---
        actions = QHBoxLayout()
        self._add_btn = QPushButton(self._std_icon(sp.SP_ArrowDown), "Add to map")
        self._add_btn.clicked.connect(self._on_add)
        self._folder_btn = QPushButton(
            self._std_icon(sp.SP_FileDialogNewFolder), "New folder")
        self._folder_btn.clicked.connect(lambda: self._c.create_folder(None))
        self._refresh_btn = QPushButton(self._std_icon(sp.SP_BrowserReload), "Refresh")
        self._refresh_btn.clicked.connect(self._c.refresh)
        actions.addWidget(self._add_btn)
        actions.addWidget(self._folder_btn)
        actions.addWidget(self._refresh_btn)
        layout.addLayout(actions)

        share = QHBoxLayout()
        self._publish_btn = QPushButton(self._std_icon(sp.SP_ArrowUp), "Publish project…")
        self._publish_btn.setToolTip("Publish the current QGIS layers as a geoi Feature Service")
        self._publish_btn.clicked.connect(self._c.publish_project)
        self._save_btn = QPushButton(
            self._std_icon(sp.SP_DialogSaveButton), "Save to geoi…")
        self._save_btn.setToolTip("Store the current QGIS project on the platform (geoi package)")
        self._save_btn.clicked.connect(self._c.save_project)
        share.addWidget(self._publish_btn)
        share.addWidget(self._save_btn)
        layout.addLayout(share)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self.setWidget(root)
        self.set_signed_out()

    # -------------------------------------------------------------- state
    def set_busy(self, text):
        self._status.setText(text or "")

    def set_signed_in(self, user):
        name = (user or {}).get("name") or (user or {}).get("email") or "Signed in"
        status = (user or {}).get("status")
        suffix = "" if status in (None, "active") else "  (account {})".format(status)
        self._account.setText("✓ {}{}".format(name, suffix))
        self._sign_btn.setText("Sign out")
        self._signed_in = True
        self._update_buttons()

    def set_signed_out(self):
        self._account.setText("Not signed in")
        self._sign_btn.setText("Sign in with Google")
        self._tree.clear()
        self._folders = []
        self._signed_in = False
        self._update_buttons()

    # -------------------------------------------------------------- content
    def populate(self, catalog):
        self._loading = True
        try:
            self._tree.clear()
            self._folders = list(catalog.get("folders", []))
            owner_id = catalog.get("ownerId")
            services = catalog.get("services", [])
            projects = catalog.get("projects", [])
            tree = content_tree.build_content_tree(
                self._folders, services, projects, owner_id=owner_id
            )
            for node in tree:
                self._tree.addTopLevelItem(self._build_item(node))
        finally:
            self._loading = False

        n_svc = len(content_tree._owned(services, owner_id))
        n_proj = len(content_tree._owned(projects, owner_id))
        if n_svc == 0 and n_proj == 0:
            self.set_busy("No content yet — publish a project or save one to get "
                          "started.")
        else:
            self.set_busy("{} services · {} projects · {} folders".format(
                n_svc, n_proj, len(self._folders)))
        self._update_buttons()

    def _std_icon(self, pixmap):
        return self.style().standardIcon(pixmap)

    def _icon(self, kind, payload=None):
        sp = QStyle.StandardPixmap
        which = {
            "folder": sp.SP_DirIcon,
            "project": sp.SP_FileDialogContentsView,
            "service": sp.SP_FileIcon,
        }.get(kind, sp.SP_FileIcon)
        return self.style().standardIcon(which)

    def _build_item(self, node):
        kind = node["kind"]
        if kind == "folder":
            item = QTreeWidgetItem([node["title"], ""])
            item.setIcon(0, self._icon("folder"))
            item.setData(0, ROLE_KIND, "folder")
            item.setData(0, ROLE_FOLDER_ID, node["id"])
            item.setData(0, ROLE_NAME, node["title"])
            item.setData(0, ROLE_PAYLOAD, node)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            for child in node["children"]:
                item.addChild(self._build_item(child))
            item.setExpanded(True)
            return item
        payload = node["payload"]
        vis = payload.get("visibility", "")
        if kind == "service" and payload.get("editable"):
            vis = (vis + " · editable").strip(" ·")
        item = QTreeWidgetItem([_title(payload), vis])
        item.setIcon(0, self._icon(kind, payload))
        item.setData(0, ROLE_KIND, kind)
        item.setData(0, ROLE_NAME, _title(payload))
        item.setData(0, ROLE_PAYLOAD, payload)
        if kind == "service":
            tip = "Double-click to add to the map · " + (vis or "private")
        else:
            tip = "Project · " + (payload.get("visibility", "") or "private")
        item.setToolTip(0, tip)
        # editable name (inline rename), but leaves are never drop targets
        flags = (item.flags() | Qt.ItemFlag.ItemIsEditable) \
            & ~Qt.ItemFlag.ItemIsDropEnabled
        item.setFlags(flags)
        return item

    def _on_item_changed(self, item, col):
        if self._loading or col != 0:
            return
        old = item.data(0, ROLE_NAME) or ""
        new = (item.text(0) or "").strip()
        if new == old:
            return
        if not new:  # empty rename — revert silently
            self._loading = True
            item.setText(0, old)
            self._loading = False
            return
        item.setData(0, ROLE_NAME, new)
        self._c.commit_rename(item.data(0, ROLE_KIND), item.data(0, ROLE_PAYLOAD), new)

    @property
    def folders(self):
        return self._folders

    def selected(self):
        items = self._tree.selectedItems()
        if not items:
            return None, None
        item = items[0]
        return item.data(0, ROLE_KIND), item.data(0, ROLE_PAYLOAD)

    # -------------------------------------------------------------- events
    def _on_sign_clicked(self):
        if getattr(self, "_signed_in", False):
            self._c.sign_out()
        else:
            self._c.sign_in()

    def _on_double_click(self, item, _col):
        if item.data(0, ROLE_KIND) == "service":
            self._c.add_service(item.data(0, ROLE_PAYLOAD))

    def _on_add(self):
        kind, payload = self.selected()
        if kind == "service":
            self._c.add_service(payload)

    def _move_to(self, item, folder_id):
        kind = item.data(0, ROLE_KIND)
        payload = item.data(0, ROLE_PAYLOAD)
        self._c.move_item(kind, payload, folder_id)

    def _on_context_menu(self, point):
        item = self._tree.itemAt(point)
        menu = QMenu(self._tree)
        if item is not None:
            kind = item.data(0, ROLE_KIND)
            payload = item.data(0, ROLE_PAYLOAD)
            if kind == "service":
                menu.addAction("Add to map", lambda: self._c.add_service(payload))
                menu.addAction("Rename", lambda: self._tree.editItem(item, 0))
                menu.addAction("Share…", lambda: self._c.share_item(kind, payload))
                menu.addAction("Move to folder…",
                               lambda: self._c.move_item_pick(kind, payload))
                menu.addAction("Copy service URL",
                               lambda: self._c.copy_service_url(payload))
                menu.addAction("Open geoi web app", self._c.open_web_app)
                menu.addSeparator()
                menu.addAction("Delete service",
                               lambda: self._c.delete_item(kind, payload))
            elif kind == "project":
                menu.addAction("Rename", lambda: self._tree.editItem(item, 0))
                menu.addAction("Share…", lambda: self._c.share_item(kind, payload))
                menu.addAction("Move to folder…",
                               lambda: self._c.move_item_pick(kind, payload))
                menu.addAction("Open geoi web app", self._c.open_web_app)
                menu.addSeparator()
                menu.addAction("Delete project",
                               lambda: self._c.delete_item(kind, payload))
            elif kind == "folder":
                fid = item.data(0, ROLE_FOLDER_ID)
                menu.addAction("New subfolder…",
                               lambda: self._c.create_folder(fid))
                menu.addAction("Rename", lambda: self._tree.editItem(item, 0))
                menu.addSeparator()
                menu.addAction("Delete folder",
                               lambda: self._c.delete_folder(fid, payload.get("title", "")))
        else:
            menu.addAction("New folder…", lambda: self._c.create_folder(None))
            menu.addAction("Refresh", self._c.refresh)
        menu.exec(self._tree.viewport().mapToGlobal(point))

    def _update_buttons(self):
        signed = getattr(self, "_signed_in", False)
        kind, _ = self.selected()
        self._add_btn.setEnabled(signed and kind == "service")
        self._folder_btn.setEnabled(signed)
        self._refresh_btn.setEnabled(signed)
        self._publish_btn.setEnabled(signed)
        self._save_btn.setEnabled(signed)


def _title(entry):
    return entry.get("title") or entry.get("name") or str(entry.get("id", ""))
