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
ROLE_SHARED = 260  # True for a "Shared with me" leaf (#981) — read-only, not owned
ROLE_DISCOVER = 261  # True for a Discover (public-search) leaf (WS3c) — read-only

# The content kinds that can be group-shared (Feature Service, web-map
# project, Tile Service, 3D-Tiles Service). A container row
# (``category``/``shared`` section header, ``folder``) is never shareable.
_SHAREABLE_KINDS = ("service", "project", "tile", "tiles3d")


def bulk_shareable(selection):
    """Filter a :meth:`ContentPanel.selected_many` list to the OWNED, shareable
    rows.

    ``selection`` is the ``[(kind, payload, shared), …]`` shape from
    ``selected_many()``. A row qualifies only when it is BOTH owned (``shared``
    falsy — a shared/not-owned item would 404 server-side on any share
    mutation) AND of a shareable kind. Pure / stdlib-only so it is unit-testable
    off QGIS.
    """
    return [
        (kind, payload, shared)
        for (kind, payload, shared) in selection
        if kind in _SHAREABLE_KINDS and not shared
    ]


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
        if kind not in ("service", "project", "folder", "tile", "tiles3d"):
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
        # QTimer / QButtonGroup / QLineEdit are imported HERE (not at module
        # scope) so ``import browser_panel`` stays compatible with the existing
        # minimal Qt test stubs — only a real panel construction needs them.
        from qgis.PyQt.QtCore import QTimer
        from qgis.PyQt.QtWidgets import QButtonGroup, QLineEdit
        self._c = controller
        self._folders = []
        # Content browsing has two modes: "mine" (the signed-in user's content)
        # and "discover" (public search). The last catalogue + last discover
        # rows are cached so switching modes never refetches.
        self._mode = "mine"
        self._last_catalog = None
        self._discover_rows = None

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

        # --- mode switch: My content | Discover -------------------------
        # A segmented, exclusive pair of checkable tool buttons. "My content"
        # (default) shows the signed-in user's tree; "Discover" reveals the
        # search box and browses geoi's PUBLIC content.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        self._mode_mine = QToolButton()
        self._mode_mine.setText("My content")
        self._mode_mine.setCheckable(True)
        self._mode_mine.setChecked(True)
        self._mode_disc = QToolButton()
        self._mode_disc.setText("Discover")
        self._mode_disc.setCheckable(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self._mode_mine, 0)
        self._mode_group.addButton(self._mode_disc, 1)
        self._mode_mine.clicked.connect(lambda: self._set_mode("mine"))
        self._mode_disc.clicked.connect(lambda: self._set_mode("discover"))
        mode_row.addWidget(self._mode_mine)
        mode_row.addWidget(self._mode_disc)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # --- Discover search box (hidden in My content) -----------------
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search public content…")
        self._search.setClearButtonEnabled(True)
        self._search.setVisible(False)
        # A leading search icon — defensive across Qt5 (QLineEdit.LeadingPosition)
        # and Qt6 (QLineEdit.ActionPosition.LeadingPosition); skipped if neither.
        try:
            pos = getattr(getattr(QLineEdit, "ActionPosition", QLineEdit),
                          "LeadingPosition")
            self._search.addAction(
                self._std_icon(QStyle.StandardPixmap.SP_FileDialogContentsView),
                pos)
        # security review: the search-box icon is purely cosmetic
        except Exception:  # nosec B110
            pass
        # ~300 ms debounce: a keystroke restarts the timer; its timeout fires
        # the off-thread DiscoverTask. Live client-side filtering of the already
        # loaded rows happens instantly on every keystroke (see
        # ``_on_search_changed``).
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._fire_discover)
        self._search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search)

        # --- content tree ---
        self._tree = _ContentTree(self)
        self._tree.setHeaderLabels(["Name", "Visibility"])
        self._tree.setColumnWidth(0, 220)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        # DESIGN: one scalable two-column QGridLayout with EQUAL column stretch,
        # so half-width siblings always line up at any panel width. The bar is
        # split into three visual groups by muted, disabled QLabel headers
        # (theme-aware, like the storage line), keeping the seven actions
        # grouped and scannable. Rows, top to bottom:
        #   0  Add to map (the single primary, full width)
        #   1  "Publish"  (muted group header)
        #   2  Feature Service (vector) | Tile Service (raster)
        #   3  3D Tiles                  (full width)
        #   4  "Manage"   (muted group header)
        #   5  Save to geoi              | New folder
        #   6  Manage groups             | Refresh
        # Consistent 16×16 icon sizing across the whole bar.
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

        def _group_label(text):
            # A muted (disabled) group header — palette-aware, like the storage
            # line — so the three action groups read as distinct sections.
            lbl = QLabel(text)
            lbl.setEnabled(False)
            return lbl

        # Row 0 — the single primary action, spanning both columns.
        self._add_btn = _cell(
            "Add to map", sp.SP_ArrowDown,
            "Add the selected feature service, web map, tile service or 3D "
            "Tiles service to the QGIS layers", self._on_add)
        actions.addWidget(self._add_btn, 0, 0, 1, 2)

        # Row 1 — Publish group header.
        actions.addWidget(_group_label("Publish"), 1, 0, 1, 2)

        # Row 2 — the two vector/raster publish actions, equal width.
        self._publish_btn = _cell(
            "Feature Service (vector)…", sp.SP_ArrowUp,
            "Publish the current vector layers as a geoi Feature Service "
            "(editable attributes + geometry)", self._c.publish_project)
        self._publish_raster_btn = _cell(
            "Tile Service (raster)…", sp.SP_ArrowUp,
            "Tile raster layers or GeoTIFFs to a cloud-native Tile Service "
            "(PMTiles, Web Mercator / EPSG:3857) and publish them to geoi",
            self._c.publish_raster)
        actions.addWidget(self._publish_btn, 2, 0)
        actions.addWidget(self._publish_raster_btn, 2, 1)

        # Row 3 — publish 3D data (span both columns): LAS/LAZ/PLY point clouds
        # (tiled by the plugin, multi-file into ONE service) or a prepared 3D
        # Tiles tileset ZIP (uploaded as-is).
        self._publish_tiles3d_btn = _cell(
            "3D Tiles…", sp.SP_ArrowUp,
            "Publish LAS / LAZ / PLY point clouds (reproject to WGS 84 or "
            "keep original coordinates, several files into one service) or "
            "upload a prepared 3D Tiles tileset ZIP (root tileset.json + "
            ".glb / .pnts) as a geoi 3D Tiles service",
            self._c.publish_tiles3d)
        actions.addWidget(self._publish_tiles3d_btn, 3, 0, 1, 2)

        # Row 4 — Manage group header.
        actions.addWidget(_group_label("Manage"), 4, 0, 1, 2)

        # Row 5 — Save + New folder, equal width.
        self._save_btn = _cell(
            "Save to geoi…", sp.SP_DialogSaveButton,
            "Store the current QGIS project on the platform (geoi package)",
            self._c.save_project)
        self._folder_btn = _cell(
            "New folder", sp.SP_FileDialogNewFolder,
            "Create a new folder", lambda: self._c.create_folder(None))
        actions.addWidget(self._save_btn, 5, 0)
        actions.addWidget(self._folder_btn, 5, 1)

        # Row 6 — Manage groups + Refresh, equal width.
        self._groups_btn = _cell(
            "Manage groups…", sp.SP_DirIcon,
            "Create, rename or delete your groups and add / remove members "
            "by email", self._c.manage_groups)
        self._refresh_btn = _cell(
            "Refresh", sp.SP_BrowserReload,
            "Reload your geoi content", self._c.refresh)
        actions.addWidget(self._groups_btn, 6, 0)
        actions.addWidget(self._refresh_btn, 6, 1)

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
        # Cache the catalogue so switching back to "My content" from Discover
        # re-renders it WITHOUT a refetch. While the user is in Discover mode a
        # background refresh must not clobber the search results — just cache.
        self._last_catalog = catalog
        if self._mode != "mine":
            return
        self._loading = True
        try:
            self._tree.clear()
            self._folders = list(catalog.get("folders", []))
            owner_id = catalog.get("ownerId")
            services = catalog.get("services", [])
            projects = catalog.get("projects", [])
            tiles = catalog.get("tiles", [])
            # 3D Tiles services — fetched fail-soft by CatalogTask into the
            # `tiles3d` key (absent/empty on servers without the feature).
            tiles3d = catalog.get("tiles3d", [])
            # Shared-with-me tile / 3D-Tiles services (#981) — fail-soft keys;
            # absent/empty on servers without the ?scope=shared endpoints.
            shared_tiles = catalog.get("sharedTiles", [])
            shared_tiles3d = catalog.get("sharedTiles3d", [])
            tree = content_tree.build_content_tree(
                self._folders, services, projects, owner_id=owner_id,
                tiles=tiles, tiles3d=tiles3d,
                shared_tiles=shared_tiles, shared_tiles3d=shared_tiles3d,
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
        "tiles3d": "tiles3d-service.svg",
        "folder": "folder.svg",
        "category": "category.svg",
        # "shared" has no bundled SVG; it falls back to a network/shared icon.
    }
    # Fall-back Qt standard pixmaps, by attribute NAME so the mapping is
    # resolved at runtime (not class-definition time, which the test stubs
    # can't satisfy). Each kind still gets a clearly-distinct standard icon.
    _ICON_FALLBACK = {
        "service": "SP_FileDialogDetailedView",
        "project": "SP_FileDialogContentsView",
        "tile": "SP_FileDialogInfoView",
        "tiles3d": "SP_ComputerIcon",
        "folder": "SP_DirIcon",
        "category": "SP_FileDialogListView",
        "shared": "SP_DriveNetIcon",
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

    def _build_item(self, node, discover=False):
        kind = node["kind"]
        if kind in ("category", "shared"):
            # A read-only label that groups content — not a real folder (no
            # rename, no drop target). "shared" is the top-level "Shared with
            # me" section (#981); "category" is a per-kind bucket. Items still
            # drop on the enclosing folder behind a category.
            item = QTreeWidgetItem([node["title"], ""])
            item.setIcon(0, self._icon(kind))
            item.setData(0, ROLE_KIND, kind)
            item.setData(0, ROLE_PAYLOAD, node)
            flags = item.flags() & ~Qt.ItemFlag.ItemIsEditable \
                & ~Qt.ItemFlag.ItemIsSelectable \
                & ~Qt.ItemFlag.ItemIsDropEnabled \
                & ~Qt.ItemFlag.ItemIsDragEnabled
            item.setFlags(flags)
            for child in node["children"]:
                item.addChild(self._build_item(child, discover=discover))
            item.setExpanded(True)
            return item
        # A "shared" leaf (Shared-with-me, #981) or a "discover" leaf (public
        # search, WS3c) is CONSUMABLE but not owned, so it is read-only: no
        # inline rename, no drag-move (the context menu also gates the
        # owner-only actions on these flags via the action registry).
        shared = bool(node.get("shared"))
        read_only = shared or discover
        if kind == "tile":
            payload = node["payload"]
            vis = payload.get("visibility", "") or "private"
            item = QTreeWidgetItem([_title(payload), vis])
            item.setIcon(0, self._icon("tile"))
            item.setData(0, ROLE_KIND, "tile")
            item.setData(0, ROLE_NAME, _title(payload))
            item.setData(0, ROLE_PAYLOAD, payload)
            item.setData(0, ROLE_SHARED, shared)
            item.setData(0, ROLE_DISCOVER, discover)
            item.setToolTip(0, ("Shared tile service · " if shared
                                else "Tile service · ") + vis)
            # Owned: inline rename + drag-move, never a drop target. Shared /
            # discovered: read-only (no rename / drag).
            flags = item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled
            if read_only:
                flags &= ~Qt.ItemFlag.ItemIsEditable \
                    & ~Qt.ItemFlag.ItemIsDragEnabled
            else:
                flags |= Qt.ItemFlag.ItemIsEditable
            item.setFlags(flags)
            return item
        if kind == "tiles3d":
            payload = node["payload"]
            vis = payload.get("visibility", "") or "private"
            item = QTreeWidgetItem([_title(payload), vis])
            item.setIcon(0, self._icon("tiles3d"))
            item.setData(0, ROLE_KIND, "tiles3d")
            item.setData(0, ROLE_NAME, _title(payload))
            item.setData(0, ROLE_PAYLOAD, payload)
            item.setData(0, ROLE_SHARED, shared)
            item.setData(0, ROLE_DISCOVER, discover)
            item.setToolTip(0, ("Shared 3D Tiles service · " if shared
                                else "3D Tiles service · ") + vis)
            # OWNED (not shared / discovered): inline rename + drag-move (the
            # rename/move client endpoints exist now — WS2); read-only
            # otherwise. Never a drop target.
            flags = item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled
            if read_only:
                flags &= ~Qt.ItemFlag.ItemIsEditable \
                    & ~Qt.ItemFlag.ItemIsDragEnabled
            else:
                flags |= Qt.ItemFlag.ItemIsEditable \
                    | Qt.ItemFlag.ItemIsDragEnabled
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
                item.addChild(self._build_item(child, discover=discover))
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
        item.setData(0, ROLE_SHARED, shared)
        item.setData(0, ROLE_DISCOVER, discover)
        if kind == "service":
            tip = "Double-click to add to the map · " + (vis or "private")
        else:
            tip = "Project · " + (payload.get("visibility", "") or "private")
        item.setToolTip(0, tip)
        # editable name (inline rename) when owned, never a drop target; a
        # discovered leaf is read-only.
        flags = item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled
        if read_only:
            flags &= ~Qt.ItemFlag.ItemIsEditable
        else:
            flags |= Qt.ItemFlag.ItemIsEditable
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

    def selected_many(self):
        """Every SHAREABLE selected leaf as ``(kind, payload, shared)`` tuples.

        Extended-selection companion to :meth:`selected`: iterates
        ``selectedItems()`` (not just the current one) and drops the
        non-shareable container rows (``category``/``shared`` section headers,
        ``folder``) so callers only see actionable content leaves. Mirrors the
        per-item data-role extraction of :meth:`selected`, adding the
        ``ROLE_SHARED`` (owned vs. shared-with-me) flag needed by the bulk
        actions.
        """
        out = []
        for item in self._tree.selectedItems():
            kind = item.data(0, ROLE_KIND)
            if kind in ("category", "shared", "folder"):
                continue
            out.append((kind, item.data(0, ROLE_PAYLOAD),
                        bool(item.data(0, ROLE_SHARED))))
        return out

    # -------------------------------------------------------------- discover
    def _set_mode(self, mode):
        """Switch between "mine" (the user's content) and "discover" (public
        search). Switching back to "mine" re-renders the CACHED catalogue with
        no refetch; switching to "discover" reveals the search box."""
        self._mode = "discover" if mode == "discover" else "mine"
        is_disc = self._mode == "discover"
        self._search.setVisible(is_disc)
        self._mode_mine.setChecked(not is_disc)
        self._mode_disc.setChecked(is_disc)
        if is_disc:
            self._discover_rows = None
            query = (self._search.text() or "").strip()
            if query:
                self._on_search_changed(query)
            else:
                self._tree.clear()
                self.set_busy("Type to search geoi's public content.")
        else:
            self._search_timer.stop()
            self._render_mine()

    def _render_mine(self):
        """Re-render the cached "My content" catalogue (no refetch)."""
        if self._last_catalog is not None:
            self.populate(self._last_catalog)
        else:
            self._tree.clear()
            self.set_busy("")

    def _on_search_changed(self, text):
        """A keystroke in the Discover box: filter the already-loaded rows
        INSTANTLY (snappy), then debounce the off-thread public search."""
        if self._mode != "discover":
            return
        query = (text or "").strip()
        if self._discover_rows is not None:
            self._render_filtered(query)
        self._search_timer.stop()
        if query:
            self._search_timer.start()
        else:
            self._tree.clear()
            self.set_busy("Type to search geoi's public content.")

    def _fire_discover(self):
        """Debounce timeout: run the off-thread public search via the
        controller for the current query (ignored when empty / not in
        Discover mode)."""
        if self._mode != "discover":
            return
        query = (self._search.text() or "").strip()
        if not query:
            return
        self.set_busy("Searching…")
        self._c.search_discover(query)

    def populate_discover(self, results, query=""):
        """Render public-search ``results`` into the SAME tree as four category
        buckets (Feature Services / Web Maps / Tile Services / 3D Tiles), with
        every leaf marked as a Discover (read-only) item. ``results`` is the
        ``{services, projects, tiles, tiles3d}`` dict from ``client.discover``.
        """
        # Drop a stale response: a slower earlier DiscoverTask (query q1) may
        # resolve AFTER a newer one (q2), which would paint q1's rows while the
        # box shows q2. Only render when this payload matches the current query.
        if query and query != (self._search.text() or "").strip():
            return
        results = results or {}
        self._discover_rows = {
            key: list(results.get(key) or [])
            for key in ("services", "projects", "tiles", "tiles3d")
        }
        self._render_discover_rows(self._discover_rows, query)

    def _render_filtered(self, query):
        """Client-side filter of the already-loaded discover rows by title —
        instant feedback while the debounced server search catches up."""
        rows = self._discover_rows or {}
        needle = (query or "").lower()

        def keep(items):
            if not needle:
                return list(items)
            return [it for it in items if needle in _title(it).lower()]

        filtered = {key: keep(rows.get(key) or [])
                    for key in ("services", "projects", "tiles", "tiles3d")}
        self._render_discover_rows(filtered, query)

    def _render_discover_rows(self, rows, query):
        """Build the four public category buckets from ``rows`` (owner-agnostic:
        no folders, no Shared-with-me section) and set the status line."""
        self._loading = True
        try:
            self._tree.clear()
            tree = content_tree.build_content_tree(
                [], rows.get("services"), rows.get("projects"),
                owner_id=None, tiles=rows.get("tiles"),
                tiles3d=rows.get("tiles3d"),
            )
            for node in tree:
                self._tree.addTopLevelItem(
                    self._build_item(node, discover=True))
        finally:
            self._loading = False
        total = sum(len(rows.get(key) or [])
                    for key in ("services", "projects", "tiles", "tiles3d"))
        if total == 0:
            self.set_busy(
                "No public content matches '{}'.".format(query) if query
                else "Type to search geoi's public content.")
        else:
            self.set_busy("{} public result{}".format(
                total, "" if total == 1 else "s"))

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
        """Add a feature service, web map (project), tile service or 3D Tiles
        service to the TOC.

        Double-click and the "Add to map" button share this so every kind is
        viewable immediately. A tile service adds as the reliable XYZ layer
        (the WMTS/PMTiles variants stay in the context menu); a web map opens
        in the controller's project flow; a 3D Tiles service adds as a native
        tiled-scene layer (QGIS 3.34+, version-gated in the controller).
        """
        if kind == "service":
            self._c.add_service(payload)
        elif kind == "tile":
            self._c.add_tile_layer(payload)
        elif kind == "tiles3d":
            self._c.add_tiles3d_layer(payload)
        elif kind == "project":
            self._c.add_project(payload)

    def _move_to(self, item, folder_id):
        kind = item.data(0, ROLE_KIND)
        payload = item.data(0, ROLE_PAYLOAD)
        self._c.move_item(kind, payload, folder_id)

    def _on_context_menu(self, point):
        item = self._tree.itemAt(point)
        menu = QMenu(self._tree)
        # MULTI-SELECT: a right-click on 2+ rows offers ONE bulk action for the
        # owned, shareable subset — never N confusing per-item menus. Falls
        # through to the normal single-item menu for one selection.
        if len(self._tree.selectedItems()) >= 2:
            eligible = bulk_shareable(self.selected_many())
            if len(eligible) >= 2:
                menu.addAction(
                    "Share {} items…".format(len(eligible)),
                    lambda: self._c.bulk_share(bulk_shareable(self.selected_many())))
                menu.exec(self._tree.viewport().mapToGlobal(point))
                return
        if item is not None:
            # SINGLE item: the menu is built DECLARATIVELY from the capability
            # registry (``_ITEM_ACTIONS``). Each action decides whether it
            # ``applies`` to this (kind, payload, shared, discover); owner-only
            # actions are skipped for a shared / discovered (not-owned) item, so
            # a new shared action added ONCE to the registry appears for every
            # applicable kind automatically.
            kind = item.data(0, ROLE_KIND)
            payload = item.data(0, ROLE_PAYLOAD) or {}
            shared = bool(item.data(0, ROLE_SHARED))
            discover = bool(item.data(0, ROLE_DISCOVER))
            if discover:
                shared = True  # a public-search item is never owner-editable
            for action in _ITEM_ACTIONS:
                if not action.applies(kind, payload, shared, discover):
                    continue
                if action.owner_only and shared:
                    continue
                if action.separator_before:
                    menu.addSeparator()
                if action.checkable:
                    act = menu.addAction(action.label)
                    act.setCheckable(True)
                    act.setChecked(
                        bool(action.checked(payload)) if action.checked
                        else False)
                    act.toggled.connect(
                        lambda checked, a=action, p=payload:
                        a.toggled(self, p, checked))
                else:
                    menu.addAction(
                        action.label,
                        lambda a=action, it=item, k=kind, p=payload:
                        a.handler(self, it, k, p))
        else:
            menu.addAction("New folder…", lambda: self._c.create_folder(None))
            menu.addAction("Manage groups…", self._c.manage_groups)
            menu.addAction("Refresh", self._c.refresh)
        menu.exec(self._tree.viewport().mapToGlobal(point))

    def _update_buttons(self):
        signed = getattr(self, "_signed_in", False)
        # "Add to map" is a single-item action: enabled only when EXACTLY one
        # addable leaf is selected (a multi-selection offers bulk share via the
        # context menu, not "Add to map" for N items).
        items = self._tree.selectedItems()
        kind = items[0].data(0, ROLE_KIND) if len(items) == 1 else None
        self._add_btn.setEnabled(
            signed and kind in ("service", "project", "tile", "tiles3d"))
        self._folder_btn.setEnabled(signed)
        self._groups_btn.setEnabled(signed)
        self._refresh_btn.setEnabled(signed)
        self._publish_btn.setEnabled(signed)
        self._save_btn.setEnabled(signed)
        self._publish_raster_btn.setEnabled(signed)
        self._publish_tiles3d_btn.setEnabled(signed)


def _title(entry):
    return entry.get("title") or entry.get("name") or str(entry.get("id", ""))


# ------------------------------------------------------------- action registry
# The single-item context menu is DATA, not a per-kind if/elif chain. Each
# ``_Action`` declares (label, applies(kind, payload, shared, discover) -> bool,
# handler, owner_only, separator_before, checkable, checked, toggled). Handlers
# receive ``(panel, item, kind, payload)`` so they can reach the controller
# (``panel._c``) and the tree (``panel._tree``); a checkable action's ``toggled``
# receives ``(panel, payload, checked)`` instead. A truly-shared action (Add to
# map, Copy URL, Open in geoi) applies to every kind where it makes sense, so
# adding one appears for all of them automatically.
class _Action:
    def __init__(self, label, applies, handler=None, *, owner_only=False,
                 separator_before=False, checkable=False, checked=None,
                 toggled=None):
        self.label = label
        self.applies = applies
        self.handler = handler
        self.owner_only = owner_only
        self.separator_before = separator_before
        self.checkable = checkable
        self.checked = checked
        self.toggled = toggled


# ----- handlers ------------------------------------------------------------
def _h_add(panel, item, kind, payload):
    panel._add_to_map(kind, payload)


def _h_rename(panel, item, kind, payload):
    panel._tree.editItem(item, 0)


def _h_share(panel, item, kind, payload):
    c = panel._c
    if kind in ("service", "project"):
        c.share_item(kind, payload)
    elif kind == "tile":
        c.share_tile_service(payload)
    elif kind == "tiles3d":
        c.share_tiles3d_service(payload)


def _h_move(panel, item, kind, payload):
    panel._c.move_item_pick(kind, payload)


def _h_delete(panel, item, kind, payload):
    panel._c.delete_item(kind, payload)


def _h_copy_service_url(panel, item, kind, payload):
    panel._c.copy_service_url(payload)


def _h_copy_tile(fmt):
    def handler(panel, item, kind, payload):
        panel._c.copy_tile_url(payload, fmt)
    return handler


def _h_copy_tiles3d(panel, item, kind, payload):
    panel._c.copy_tiles3d_url(payload)


def _h_copy_url_generic(panel, item, kind, payload):
    panel._c.copy_public_url(kind, payload)


def _h_open_web(panel, item, kind, payload):
    panel._c.open_web_app()


def _h_open_tile(panel, item, kind, payload):
    panel._c.open_tile_in_web_app(payload)


def _h_open_deck(panel, item, kind, payload):
    panel._c.open_tiles3d_preview_in_web_app(payload, engine="deck")


def _h_open_cesium(panel, item, kind, payload):
    panel._c.open_tiles3d_preview_in_web_app(payload, engine="cesium")


def _h_open_generic(panel, item, kind, payload):
    panel._c.open_public_in_web_app(kind, payload)


def _h_add_xyz(panel, item, kind, payload):
    panel._c.add_tile_layer(payload)


def _h_add_wmts(panel, item, kind, payload):
    panel._c.add_tile_wmts_layer(payload)


def _h_add_tiles3d(panel, item, kind, payload):
    panel._c.add_tiles3d_layer(payload)


def _h_new_subfolder(panel, item, kind, payload):
    panel._c.create_folder(item.data(0, ROLE_FOLDER_ID))


def _h_delete_folder(panel, item, kind, payload):
    fid = item.data(0, ROLE_FOLDER_ID)
    panel._c.delete_folder(fid, (payload or {}).get("title", ""))


def _t_editable(panel, payload, checked):
    panel._c.set_service_editable(payload, checked)


# ----- the ordered registry ------------------------------------------------
_ITEM_ACTIONS = [
    # Add to map — shared: service/project always, and ANY discovered item
    # (dispatched per kind by ``_add_to_map``).
    _Action("Add to map",
            lambda k, p, s, d: bool(d) or k in ("service", "project"),
            _h_add),
    _Action("Add as XYZ layer",
            lambda k, p, s, d: k == "tile" and not d, _h_add_xyz),
    _Action("Add as WMTS layer",
            lambda k, p, s, d: k == "tile" and not d, _h_add_wmts),
    _Action("Add to map (QGIS 3.34+)",
            lambda k, p, s, d: k == "tiles3d" and not d, _h_add_tiles3d),
    _Action("New subfolder…",
            lambda k, p, s, d: k == "folder", _h_new_subfolder),
    # Rename / Share / Editable / Move — owner-only (hidden for shared/discover).
    _Action("Rename",
            lambda k, p, s, d: k in ("service", "project", "tile",
                                     "tiles3d", "folder"),
            _h_rename, owner_only=True),
    _Action("Share…",
            lambda k, p, s, d: k in ("service", "project", "tile", "tiles3d"),
            _h_share, owner_only=True),
    _Action("Editable (read/write)",
            lambda k, p, s, d: k == "service",
            owner_only=True, checkable=True,
            checked=lambda p: bool((p or {}).get("editable")),
            toggled=_t_editable),
    _Action("Move to folder…",
            lambda k, p, s, d: k in ("service", "project", "tile", "tiles3d"),
            _h_move, owner_only=True),
    # Copy URL(s) — per-kind for owned items; a generic "Copy URL" for discover.
    _Action("Copy service URL",
            lambda k, p, s, d: k == "service" and not d, _h_copy_service_url),
    _Action("Copy XYZ URL",
            lambda k, p, s, d: k == "tile" and not d, _h_copy_tile("xyz")),
    _Action("Copy WMTS URL",
            lambda k, p, s, d: k == "tile" and not d, _h_copy_tile("wmts")),
    _Action("Copy PMTiles URL",
            lambda k, p, s, d: k == "tile" and not d, _h_copy_tile("pmtiles")),
    _Action("Copy tileset URL",
            lambda k, p, s, d: k == "tiles3d" and not d, _h_copy_tiles3d),
    _Action("Copy URL", lambda k, p, s, d: bool(d), _h_copy_url_generic),
    # Open in the geoi web app — per-kind for owned; a generic "Open in geoi"
    # for discovered items.
    _Action("Open geoi web app",
            lambda k, p, s, d: k in ("service", "project") and not d,
            _h_open_web),
    _Action("Open in geoi web app",
            lambda k, p, s, d: k == "tile" and not d, _h_open_tile),
    _Action("Open deck.gl preview in web app",
            lambda k, p, s, d: k == "tiles3d" and not d, _h_open_deck),
    _Action("Open Cesium preview in web app",
            lambda k, p, s, d: k == "tiles3d" and not d, _h_open_cesium),
    _Action("Open in geoi", lambda k, p, s, d: bool(d), _h_open_generic),
    # Delete — owner-only, separated from the rest.
    _Action("Delete service",
            lambda k, p, s, d: k == "service", _h_delete,
            owner_only=True, separator_before=True),
    _Action("Delete project",
            lambda k, p, s, d: k == "project", _h_delete,
            owner_only=True, separator_before=True),
    _Action("Delete tile service",
            lambda k, p, s, d: k == "tile", _h_delete,
            owner_only=True, separator_before=True),
    _Action("Delete 3D Tiles service",
            lambda k, p, s, d: k == "tiles3d", _h_delete,
            owner_only=True, separator_before=True),
    _Action("Delete folder",
            lambda k, p, s, d: k == "folder", _h_delete_folder,
            separator_before=True),
]


def actions_for(kind, *, shared=False, discover=False, payload=None):
    """The ordered list of context-menu action LABELS the registry yields for
    a single item — pure (no Qt), so the menu contract is unit-testable. A
    discovered item is always treated as not-owned (``shared=True``)."""
    payload = payload or {}
    if discover:
        shared = True
    out = []
    for action in _ITEM_ACTIONS:
        if not action.applies(kind, payload, shared, discover):
            continue
        if action.owner_only and shared:
            continue
        out.append(action.label)
    return out
