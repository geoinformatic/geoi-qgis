"""The dockable geoi panel: sign in, browse your content in folders, act on it."""

import os

from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
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

# Bundled per-kind icons (see ``_icon``). Loaded with ``QIcon(path)`` and a
# Qt-standard-pixmap fall-back, so a missing/unsupported SVG never breaks the
# tree on any QGIS version.
_ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons")

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
        if kind not in ("service", "project", "folder", "tile"):
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
        self._sign_btn = QPushButton("Sign in")
        self._sign_btn.clicked.connect(self._on_sign_clicked)
        self._settings_btn = QToolButton()
        self._settings_btn.setIcon(self._std_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self._settings_btn.setToolTip("Server settings")
        self._settings_btn.clicked.connect(self._c.open_settings)
        acct.addWidget(self._account, 1)
        acct.addWidget(self._sign_btn)
        acct.addWidget(self._settings_btn)
        layout.addLayout(acct)

        # --- storage overview (#677) ---
        # A small muted line under the account row: "<used> of <quota> used
        # (<pct>%)" (or "<used> used" when the account is unlimited). Hidden
        # until the post-sign-in content load reports it; a failed/absent
        # /hub/storage just leaves it hidden — it never blocks sign-in.
        self._storage = QLabel("")
        self._storage.setWordWrap(True)
        self._storage.setEnabled(False)  # muted (disabled text colour, theme-aware)
        self._storage.setVisible(False)
        layout.addWidget(self._storage)

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
        # --- action area -------------------------------------------------
        # DESIGN: a single, scalable QGridLayout instead of three ragged
        # QHBoxLayout rows. The old layout put the two Publish buttons in
        # different rows with different sibling counts, so they rendered at
        # different widths and looked unbalanced. Here every button shares the
        # same two-column grid with equal column stretch, so siblings always
        # line up at any panel width — the two Publish actions in particular
        # are guaranteed equal width because they sit in the same row, one per
        # column. Rows, top to bottom:
        #   0  Add to map (primary, full width — the most common action)
        #   1  Publish: Feature Service (vector) | Tile Service (raster)
        #   2  Save to geoi              | New folder
        #   3  Refresh (full width)
        # Consistent icon sizing across the whole bar.
        icon_size = QSize(16, 16)
        actions = QGridLayout()
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(6)
        actions.setColumnStretch(0, 1)
        actions.setColumnStretch(1, 1)

        def _cell(text, icon, tip, slot):
            btn = QPushButton(self._std_icon(icon), text)
            btn.setIconSize(icon_size)
            btn.setToolTip(tip)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            btn.clicked.connect(slot)
            return btn

        # Row 0 — primary action, spanning both columns.
        self._add_btn = _cell(
            "Add to map", sp.SP_ArrowDown,
            "Add the selected feature service, web map or tile service to the "
            "QGIS layers", self._on_add)
        actions.addWidget(self._add_btn, 0, 0, 1, 2)

        # Row 1 — the two publish actions, equal width side by side.
        self._publish_btn = _cell(
            "Publish: Feature Service (vector)…", sp.SP_ArrowUp,
            "Publish the current vector layers as a geoi Feature Service "
            "(editable attributes + geometry)", self._c.publish_project)
        self._publish_raster_btn = _cell(
            "Publish: Tile Service (raster)…", sp.SP_ArrowUp,
            "Tile raster layers or GeoTIFFs to a cloud-native Tile Service "
            "(PMTiles, Web Mercator / EPSG:3857) and publish them to geoi",
            self._c.publish_raster)
        actions.addWidget(self._publish_btn, 1, 0)
        actions.addWidget(self._publish_raster_btn, 1, 1)

        # Row 2 — Save + New folder.
        self._save_btn = _cell(
            "Save to geoi…", sp.SP_DialogSaveButton,
            "Store the current QGIS project on the platform (geoi package)",
            self._c.save_project)
        self._folder_btn = _cell(
            "New folder", sp.SP_FileDialogNewFolder,
            "Create a new folder", lambda: self._c.create_folder(None))
        actions.addWidget(self._save_btn, 2, 0)
        actions.addWidget(self._folder_btn, 2, 1)

        # Row 3 — Refresh, spanning both columns.
        self._refresh_btn = _cell(
            "Refresh", sp.SP_BrowserReload,
            "Reload your geoi content", self._c.refresh)
        actions.addWidget(self._refresh_btn, 3, 0, 1, 2)

        layout.addLayout(actions)

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
        self._sign_btn.setText("Sign in")
        self._tree.clear()
        self._folders = []
        self._signed_in = False
        self.set_storage(None)
        self._update_buttons()

    def set_storage(self, storage):
        """Show/refresh the muted storage-overview line under the account row.

        ``storage`` is a ``/hub/storage`` envelope (or ``None`` to clear). Pure
        formatting lives in ``geoi_client.storage_overview`` so it is unit-
        tested off QGIS; an empty line hides the label. A breakdown, when
        present, becomes a tooltip (feature / tiles / web maps / apps).
        """
        from ..geoi_client import fmt_bytes, storage_overview

        line = storage_overview(storage)
        self._storage.setText(line)
        self._storage.setVisible(bool(line))
        tip = ""
        breakdown = (storage or {}).get("breakdown") if isinstance(storage, dict) else None
        if isinstance(breakdown, dict):
            parts = []
            for key, label in (("feature", "Features"), ("tile", "Tiles"),
                               ("project", "Web maps"), ("app", "Apps")):
                if key in breakdown:
                    parts.append("{}: {}".format(label, fmt_bytes(breakdown.get(key))))
            tip = "\n".join(parts)
        self._storage.setToolTip(tip)

    # -------------------------------------------------------------- content
    def populate(self, catalog):
        self._loading = True
        try:
            self._tree.clear()
            self._folders = list(catalog.get("folders", []))
            owner_id = catalog.get("ownerId")
            services = catalog.get("services", [])
            projects = catalog.get("projects", [])
            tiles = catalog.get("tiles", [])
            tree = content_tree.build_content_tree(
                self._folders, services, projects, owner_id=owner_id,
                tiles=tiles,
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

    # Each content kind gets a DISTINCT, self-explanatory icon so feature
    # services / web maps / tile services are recognisable at a glance. We
    # bundle small SVGs (``geoi/icons/``) — robust across QGIS versions and
    # packed into the plugin zip — and fall back to a clearly-distinct Qt
    # standard pixmap if a file is missing or the SVG can't be rendered.
    _ICON_FILES = {
        "service": "feature-service.svg",
        "project": "web-map.svg",
        "tile": "tile-service.svg",
        "folder": "folder.svg",
        "category": "category.svg",
    }
    # Fall-back Qt standard pixmaps, by attribute NAME so the mapping is
    # resolved at runtime (not class-definition time, which the test stubs
    # can't satisfy). Each kind still gets a clearly-distinct standard icon.
    _ICON_FALLBACK = {
        "service": "SP_FileDialogDetailedView",
        "project": "SP_FileDialogContentsView",
        "tile": "SP_FileDialogInfoView",
        "folder": "SP_DirIcon",
        "category": "SP_FileDialogListView",
    }

    def _icon(self, kind, payload=None):
        cache = getattr(self, "_icon_cache", None)
        if cache is None:
            cache = self._icon_cache = {}
        if kind in cache:
            return cache[kind]
        icon = None
        fname = self._ICON_FILES.get(kind)
        if fname:
            path = os.path.join(_ICON_DIR, fname)
            if os.path.exists(path):
                candidate = QIcon(path)
                if not candidate.isNull():
                    icon = candidate
        if icon is None:
            sp = self._ICON_FALLBACK.get(kind, "SP_FileIcon")
            pixmap = getattr(QStyle.StandardPixmap, sp,
                             QStyle.StandardPixmap.SP_FileIcon)
            icon = self.style().standardIcon(pixmap)
        cache[kind] = icon
        return icon

    def _build_item(self, node):
        kind = node["kind"]
        if kind == "category":
            # A read-only label that groups one kind of content (Feature
            # Services / Tile Services) — not a real folder (no rename, no drop
            # target). Items still drop on the enclosing folder behind it.
            item = QTreeWidgetItem([node["title"], ""])
            item.setIcon(0, self._icon("category"))
            item.setData(0, ROLE_KIND, "category")
            item.setData(0, ROLE_PAYLOAD, node)
            flags = item.flags() & ~Qt.ItemFlag.ItemIsEditable \
                & ~Qt.ItemFlag.ItemIsSelectable \
                & ~Qt.ItemFlag.ItemIsDropEnabled \
                & ~Qt.ItemFlag.ItemIsDragEnabled
            item.setFlags(flags)
            for child in node["children"]:
                item.addChild(self._build_item(child))
            item.setExpanded(True)
            return item
        if kind == "tile":
            payload = node["payload"]
            vis = payload.get("visibility", "") or "private"
            item = QTreeWidgetItem([_title(payload), vis])
            item.setIcon(0, self._icon("tile"))
            item.setData(0, ROLE_KIND, "tile")
            item.setData(0, ROLE_NAME, _title(payload))
            item.setData(0, ROLE_PAYLOAD, payload)
            item.setToolTip(0, "Tile service · " + vis)
            # Same affordances as a feature service: inline rename + drag-move,
            # never a drop target.
            flags = (item.flags() | Qt.ItemFlag.ItemIsEditable) \
                & ~Qt.ItemFlag.ItemIsDropEnabled
            item.setFlags(flags)
            return item
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
        self._add_to_map(item.data(0, ROLE_KIND), item.data(0, ROLE_PAYLOAD))

    def _on_add(self):
        kind, payload = self.selected()
        self._add_to_map(kind, payload)

    def _add_to_map(self, kind, payload):
        """Add a feature service, web map (project) or tile service to the TOC.

        Double-click and the "Add to map" button share this so every kind is
        viewable immediately. A tile service adds as the reliable XYZ layer
        (the WMTS/PMTiles variants stay in the context menu); a web map opens
        in the controller's project flow.
        """
        if kind == "service":
            self._c.add_service(payload)
        elif kind == "tile":
            self._c.add_tile_layer(payload)
        elif kind == "project":
            self._c.add_project(payload)

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
                menu.addAction("Add to map", lambda: self._c.add_project(payload))
                menu.addAction("Rename", lambda: self._tree.editItem(item, 0))
                menu.addAction("Share…", lambda: self._c.share_item(kind, payload))
                menu.addAction("Move to folder…",
                               lambda: self._c.move_item_pick(kind, payload))
                menu.addAction("Open geoi web app", self._c.open_web_app)
                menu.addSeparator()
                menu.addAction("Delete project",
                               lambda: self._c.delete_item(kind, payload))
            elif kind == "tile":
                menu.addAction("Add as XYZ layer",
                               lambda: self._c.add_tile_layer(payload))
                menu.addAction("Add as WMTS layer",
                               lambda: self._c.add_tile_wmts_layer(payload))
                menu.addAction("Rename", lambda: self._tree.editItem(item, 0))
                menu.addAction("Share…",
                               lambda: self._c.share_tile_service(payload))
                menu.addAction("Move to folder…",
                               lambda: self._c.move_item_pick(kind, payload))
                menu.addAction("Copy XYZ URL",
                               lambda: self._c.copy_tile_url(payload, "xyz"))
                menu.addAction("Copy WMTS URL",
                               lambda: self._c.copy_tile_url(payload, "wmts"))
                # PMTiles: QGIS has no reliable add-by-URL for RASTER PMTiles
                # archives (see plugin.add_tile_pmtiles_layer note), so we only
                # offer the copy action.
                menu.addAction("Copy PMTiles URL",
                               lambda: self._c.copy_tile_url(payload, "pmtiles"))
                menu.addAction("Open in geoi web app",
                               lambda: self._c.open_tile_in_web_app(payload))
                menu.addSeparator()
                menu.addAction("Delete tile service",
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
        self._add_btn.setEnabled(signed and kind in ("service", "project", "tile"))
        self._folder_btn.setEnabled(signed)
        self._refresh_btn.setEnabled(signed)
        self._publish_btn.setEnabled(signed)
        self._save_btn.setEnabled(signed)
        self._publish_raster_btn.setEnabled(signed)


def _title(entry):
    return entry.get("title") or entry.get("name") or str(entry.get("id", ""))
