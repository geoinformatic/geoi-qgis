"""Small modal dialogs: server settings, publish, save-to-geoi."""

import os

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)
from qgis.PyQt.QtCore import Qt

from .. import settings
from ..geoi_client import GeoiError

_SB = QDialogButtonBox.StandardButton
_UR = Qt.ItemDataRole.UserRole


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("geoi — server settings")
        form = QFormLayout(self)
        self._base = QLineEdit(settings.base_url())
        self._base.setPlaceholderText(settings.DEFAULT_BASE_URL)
        form.addRow("Platform URL", self._base)
        hint = QLabel(
            "The geoi platform to sign in to. Sign-in reuses your platform's own "
            "web sign-in — whichever of Google, Apple or Microsoft your admin "
            "enabled — with no other configuration needed."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        settings.set_base_url(self._base.text())
        self.accept()


class PublishDialog(QDialog):
    """Choose layers + service name + who can see and edit the new service."""

    def __init__(self, layers_info, default_name="My layers", groups=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Publish as Feature Service (vector)")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Service name"))
        self._name = QLineEdit(default_name)
        layout.addWidget(self._name)

        layout.addWidget(QLabel("Layers to include — name and how they're styled"))
        self._list = QListWidget()
        for info in layers_info:
            summary = info.get("summary", "")
            text = info.get("name", "")
            if summary:
                text += "\n" + summary
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, info.get("id"))
            item.setToolTip(summary)
            self._list.addItem(item)
        layout.addWidget(self._list)

        # --- access options ---
        form = QFormLayout()
        self._visibility = QComboBox()
        # label -> value; "groups" only makes sense when the user has groups
        self._visibility.addItem("Private (only me)", "private")
        if groups:
            self._visibility.addItem("Shared with a group", "groups")
        self._visibility.addItem("Public (anyone)", "public")
        self._visibility.currentIndexChanged.connect(self._sync_groups)
        form.addRow("Visibility", self._visibility)

        self._editable = QCheckBox("Allow editing the features")
        form.addRow("", self._editable)
        layout.addLayout(form)

        self._groups_label = QLabel("Share with")
        layout.addWidget(self._groups_label)
        self._groups = QListWidget()
        for group in groups or []:
            item = QListWidgetItem(group.get("name", "Group"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, group.get("id"))
            self._groups.addItem(item)
        layout.addWidget(self._groups)

        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.button(_SB.Ok).setText("Publish")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_groups()

    def _sync_groups(self):
        show = self.visibility() == "groups"
        self._groups_label.setVisible(show)
        self._groups.setVisible(show)

    def service_name(self):
        return self._name.text().strip()

    def visibility(self):
        return self._visibility.currentData() or "private"

    def editable(self):
        return self._editable.isChecked()

    def selected_group_ids(self):
        ids = []
        for row in range(self._groups.count()):
            item = self._groups.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids

    def selected_layer_ids(self):
        ids = []
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids


class PublishRasterDialog(QDialog):
    """Choose raster sources + a name to publish as cloud-native PMTiles.

    Sources are EITHER a folder of GeoTIFFs OR the project's loaded raster
    layers (checked). The target CRS is **Web Mercator (EPSG:3857), fixed** —
    shown as a read-only label, with NO editable CRS widget.
    """

    def __init__(self, raster_layers_info=None, default_name="My tiles", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Publish as Tile Service (raster)")
        self.setMinimumWidth(440)
        self._folder = ""
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Tile set name"))
        self._name = QLineEdit(default_name)
        layout.addWidget(self._name)

        # --- source: loaded raster layers ---
        layout.addWidget(QLabel("Raster layers to tile"))
        self._list = QListWidget()
        for info in raster_layers_info or []:
            item = QListWidgetItem(info.get("name", ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, info.get("source"))
            self._list.addItem(item)
        layout.addWidget(self._list)

        # --- source: a folder of GeoTIFFs ---
        self._folder_btn = QPushButton("Choose a folder of GeoTIFFs…")
        self._folder_btn.clicked.connect(self._pick_folder)
        layout.addWidget(self._folder_btn)
        self._folder_label = QLabel("No folder chosen")
        self._folder_label.setWordWrap(True)
        layout.addWidget(self._folder_label)

        # --- fixed CRS (read-only, no widget to change it) ---
        crs = QLabel("CRS: Web Mercator (EPSG:3857) — fixed")
        crs.setWordWrap(True)
        layout.addWidget(crs)

        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.button(_SB.Ok).setText("Publish tiles")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a folder of GeoTIFFs")
        if not path:
            return
        self._folder = path
        self._folder_label.setText(path)

    def _on_accept(self):
        if not self.sources():
            QMessageBox.warning(
                self, "geoi",
                "Choose at least one raster layer or a folder of GeoTIFFs.")
            return
        self.accept()

    def tile_name(self):
        return self._name.text().strip()

    def sources(self):
        """The chosen raster sources: a folder path plus each checked layer's
        file source. Empty entries are dropped."""
        out = []
        if self._folder:
            out.append(self._folder)
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                source = item.data(Qt.ItemDataRole.UserRole)
                if source:
                    out.append(source)
        return out


class Tiles3dCrsChoiceDialog(QDialog):
    """Per-file coordinate handling when publishing point clouds as 3D Tiles.

    ``files_info`` is ``[{"path", "name", "crs"?}]`` — ``crs`` is the cheaply
    pre-detected source CRS (e.g. ``"EPSG:25832"`` from a LAS/LAZ header) or
    ``None`` when it is only resolvable at publish time. For EACH file the
    user picks a placement (the values ``tiles3d.publish_point_cloud`` takes):

      * ``"reproject"`` — reproject to WGS 84 (recommended): the tileset
        lands at its real location on the globe;
      * ``"local"`` — keep the original coordinates (local placement): for
        scenes with no usable CRS.

    Plus ONE overall service title (defaulting to the first file's stem).
    ``choices()`` returns ``[{"path", "placement"}]`` in the input order.
    Palette-aware: standard widgets only, no hardcoded colours.
    """

    PLACEMENT_REPROJECT = "reproject"
    PLACEMENT_LOCAL = "local"

    def __init__(self, files_info, default_title="3D tiles", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Publish point clouds as 3D Tiles")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._title = QLineEdit(default_title)
        form.addRow("Service title", self._title)
        layout.addLayout(form)

        layout.addWidget(QLabel("Coordinates — choose per file"))
        files_form = QFormLayout()
        self._rows = []  # [(path, QComboBox)]
        for info in files_info or []:
            name = info.get("name") or os.path.basename(info.get("path", ""))
            crs = info.get("crs")
            label = QLabel("{}\n{}".format(
                name, crs or "CRS detected at publish time"))
            label.setWordWrap(True)
            combo = QComboBox()
            combo.addItem("Reproject to WGS 84 (recommended)",
                          self.PLACEMENT_REPROJECT)
            combo.addItem("Keep original coordinates (local placement)",
                          self.PLACEMENT_LOCAL)
            files_form.addRow(label, combo)
            self._rows.append((info.get("path"), combo))
        layout.addLayout(files_form)

        hint = QLabel(
            "Reproject puts the data at its real place on the globe. Keep "
            "original shows it as-is at a local origin (no/unknown CRS)."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.button(_SB.Ok).setText("Publish")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def title(self):
        return self._title.text().strip()

    def choices(self):
        """[{"path", "placement"}] — one entry per input file, in order."""
        out = []
        for path, combo in self._rows:
            out.append({
                "path": path,
                "placement": combo.currentData() or self.PLACEMENT_REPROJECT,
            })
        return out


class SaveProjectDialog(QDialog):
    def __init__(self, default_name="QGIS project", layers_info=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Save project to geoi")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name = QLineEdit(default_name)
        form.addRow("Project name", self._name)
        self._visibility = QComboBox()
        self._visibility.addItems(["private", "groups", "public"])
        form.addRow("Visibility", self._visibility)
        layout.addLayout(form)

        if layers_info:
            layout.addWidget(QLabel("Layers in this project — name and how they're styled"))
            preview = QListWidget()
            preview.setSelectionMode(preview.SelectionMode.NoSelection)
            for info in layers_info:
                summary = info.get("summary", "")
                text = info.get("name", "")
                if summary:
                    text += "\n" + summary
                item = QListWidgetItem(text)
                item.setToolTip(summary)
                preview.addItem(item)
            layout.addWidget(preview)

        note = QLabel(
            "Saves the current layers as a geoi project (a 'geoi package') that "
            "re-opens in the geoi app and in this plugin, with the same styling."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.button(_SB.Ok).setText("Save")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def project_name(self):
        return self._name.text().strip()

    def visibility(self):
        return self._visibility.currentText()


class ShareDialog(QDialog):
    """Pick visibility (private / groups / public) and, for ``groups``, which
    of the caller's groups a service or project is shared with.

    ``groups`` is the hub list ({id, name}); ``shared_ids`` are the groups it
    is shared with already; ``current`` is its current visibility.
    """

    def __init__(self, title, groups, shared_ids=None, current="private", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        shared = set(shared_ids or [])

        layout.addWidget(QLabel("Who can see this"))
        self._visibility = QComboBox()
        self._visibility.addItem("Private (only me)", "private")
        self._visibility.addItem("Shared with a group", "groups")
        self._visibility.addItem("Public (anyone)", "public")
        idx = self._visibility.findData(current)
        self._visibility.setCurrentIndex(idx if idx >= 0 else 0)
        self._visibility.currentIndexChanged.connect(self._sync)
        layout.addWidget(self._visibility)

        self._groups_label = QLabel("Groups")
        layout.addWidget(self._groups_label)
        self._groups = QListWidget()
        for group in groups or []:
            item = QListWidgetItem(group.get("name", "Group"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = group.get("id") in shared
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, group.get("id"))
            self._groups.addItem(item)
        layout.addWidget(self._groups)
        if not groups:
            self._groups_label.setText("You are not in any groups yet — create "
                                       "one in the geoi web app.")

        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.button(_SB.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync()

    def _sync(self):
        show = self.visibility() == "groups"
        self._groups_label.setVisible(show)
        self._groups.setVisible(show)

    def visibility(self):
        return self._visibility.currentData() or "private"

    def selected_group_ids(self):
        ids = []
        for row in range(self._groups.count()):
            item = self._groups.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids


class ManageGroupsDialog(QDialog):
    """Create, rename and delete groups, and add/remove members by email.

    ``client`` is a :class:`GeoiClient` (live calls — group management is
    low-frequency and small, so it runs synchronously here); ``groups`` is the
    caller's group list (``list_groups``: ``{id, name, myRole}``). Owner-only
    actions (rename, delete, add/remove member) are DISABLED for a group the
    caller does not own — mirroring how ``browser_panel`` hides owner-only
    actions on shared items. Server 403s (and any other error) surface through
    a warning box, and the group/member lists reload after every change.
    """

    def __init__(self, client, groups, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage groups")
        self.setMinimumWidth(460)
        self._client = client
        self._groups = list(groups or [])
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Your groups"))
        self._group_list = QListWidget()
        self._group_list.currentItemChanged.connect(
            lambda *_a: self._refresh_actions())
        self._group_list.currentItemChanged.connect(
            lambda *_a: self._load_members())
        layout.addWidget(self._group_list)

        group_row = QHBoxLayout()
        self._create_btn = QPushButton("Create…")
        self._create_btn.clicked.connect(self._create_group)
        self._rename_btn = QPushButton("Rename…")
        self._rename_btn.clicked.connect(self._rename_group)
        self._delete_btn = QPushButton("Delete…")
        self._delete_btn.clicked.connect(self._delete_group)
        group_row.addWidget(self._create_btn)
        group_row.addWidget(self._rename_btn)
        group_row.addWidget(self._delete_btn)
        layout.addLayout(group_row)

        self._members_label = QLabel("Members")
        layout.addWidget(self._members_label)
        self._member_list = QListWidget()
        layout.addWidget(self._member_list)

        member_row = QHBoxLayout()
        self._add_member_btn = QPushButton("Add member…")
        self._add_member_btn.clicked.connect(self._add_member)
        self._remove_member_btn = QPushButton("Remove member")
        self._remove_member_btn.clicked.connect(self._remove_member)
        member_row.addWidget(self._add_member_btn)
        member_row.addWidget(self._remove_member_btn)
        layout.addLayout(member_row)

        buttons = QDialogButtonBox(_SB.Close)
        buttons.rejected.connect(self.accept)
        close_btn = buttons.button(_SB.Close)
        if close_btn is not None:
            close_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._fill_groups()
        self._refresh_actions()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _is_owner(group):
        """True when the caller owns ``group`` (``myRole == 'owner'``)."""
        return bool(group) and str(group.get("myRole")) == "owner"

    def _selected_group(self):
        item = self._group_list.currentItem()
        if item is None:
            return None
        data = item.data(_UR)
        return data if isinstance(data, dict) else None

    def _selected_member(self):
        item = self._member_list.currentItem()
        if item is None:
            return None
        data = item.data(_UR)
        return data if isinstance(data, dict) else None

    def _refresh_actions(self):
        """Enable owner-only actions only for a group the caller owns."""
        group = self._selected_group()
        owner = self._is_owner(group)
        self._rename_btn.setEnabled(owner)
        self._delete_btn.setEnabled(owner)
        self._add_member_btn.setEnabled(owner)
        self._remove_member_btn.setEnabled(owner)

    def _fill_groups(self, select_id=None):
        self._group_list.clear()
        target_row = 0
        for i, group in enumerate(self._groups):
            role = group.get("myRole")
            label = group.get("name", "Group")
            if role and role != "owner":
                label += "  (member)"
            item = QListWidgetItem(label)
            item.setData(_UR, group)
            self._group_list.addItem(item)
            if select_id is not None and group.get("id") == select_id:
                target_row = i
        if self._groups:
            self._group_list.setCurrentRow(target_row)
        else:
            self._member_list.clear()
        self._refresh_actions()

    def _reload_groups(self, select_id=None):
        try:
            self._groups = list(self._client.list_groups())
        except GeoiError as exc:
            self._warn(exc)
            return
        self._fill_groups(select_id=select_id)

    def _load_members(self):
        self._member_list.clear()
        group = self._selected_group()
        if not group:
            return
        try:
            detail = self._client.get_group(group.get("id"))
        except GeoiError as exc:
            self._warn(exc)
            return
        # The detail's myRole is authoritative — reflect it onto the row so the
        # owner-only gating is right even if the list was stale.
        info = detail.get("group") if isinstance(detail, dict) else None
        if isinstance(info, dict) and info.get("myRole") is not None:
            group["myRole"] = info.get("myRole")
            self._refresh_actions()
        for member in (detail.get("members") or []):
            role = member.get("role", "")
            email = member.get("email") or member.get("name") or ""
            label = "{}  ·  {}".format(email, role) if role else email
            item = QListWidgetItem(label)
            item.setData(_UR, member)
            self._member_list.addItem(item)

    def _warn(self, exc):
        QMessageBox.warning(
            self, "geoi", str(getattr(exc, "message", None) or exc))

    # -------------------------------------------------------------- actions
    def _create_group(self):
        name, ok = QInputDialog.getText(self, "Create group", "Group name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        try:
            group = self._client.create_group(name)
        except GeoiError as exc:
            return self._warn(exc)
        self._reload_groups(select_id=(group or {}).get("id"))

    def _rename_group(self):
        group = self._selected_group()
        if not self._is_owner(group):
            return
        name, ok = QInputDialog.getText(
            self, "Rename group", "New name:", text=group.get("name", ""))
        name = (name or "").strip()
        if not ok or not name:
            return
        try:
            self._client.rename_group(group.get("id"), name)
        except GeoiError as exc:
            return self._warn(exc)
        self._reload_groups(select_id=group.get("id"))

    def _delete_group(self):
        group = self._selected_group()
        if not self._is_owner(group):
            return
        if QMessageBox.question(
            self, "Delete group",
            "Delete group '{}'? This cannot be undone.".format(
                group.get("name", "")),
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._client.delete_group(group.get("id"))
        except GeoiError as exc:
            return self._warn(exc)
        self._reload_groups()

    def _add_member(self):
        group = self._selected_group()
        if not self._is_owner(group):
            return
        email, ok = QInputDialog.getText(
            self, "Add member", "Member's email address:")
        email = (email or "").strip()
        if not ok or not email:
            return
        try:
            self._client.add_group_member(group.get("id"), email)
        except GeoiError as exc:
            return self._warn(exc)
        self._load_members()

    def _remove_member(self):
        group = self._selected_group()
        if not self._is_owner(group):
            return
        member = self._selected_member()
        if not member:
            return
        uid = member.get("userId")
        if uid in (None, ""):
            return
        if QMessageBox.question(
            self, "Remove member",
            "Remove {} from '{}'?".format(
                member.get("email") or member.get("name") or "this member",
                group.get("name", "")),
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._client.remove_group_member(group.get("id"), uid)
        except GeoiError as exc:
            return self._warn(exc)
        self._load_members()


class MoveToFolderDialog(QDialog):
    """Pick a destination folder (or the root) for a service or project.

    ``folders`` is the flat hub list ({id, parentId, title}); it is shown as
    an indented tree. ``selected_folder_id()`` returns a folder id, or "" for
    the root.
    """

    def __init__(self, folders, current_folder_id=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Move to folder")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Destination"))
        self._combo = QComboBox()
        self._combo.addItem("(Root)", "")
        for label, fid in _indented_folders(folders):
            self._combo.addItem(label, fid)
        if current_folder_id:
            idx = self._combo.findData(current_folder_id)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
        layout.addWidget(self._combo)
        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.button(_SB.Ok).setText("Move")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_folder_id(self):
        return self._combo.currentData() or ""


def _indented_folders(folders):
    """Yield (indented title, id) pairs in tree order for a flat folder list."""
    by_parent = {}
    for f in folders or []:
        by_parent.setdefault(f.get("parentId"), []).append(f)
    known = {f.get("id") for f in folders or []}

    out = []

    def walk(parent, depth):
        children = sorted(by_parent.get(parent, []),
                          key=lambda f: (f.get("title") or "").lower())
        for f in children:
            out.append(("    " * depth + (f.get("title") or "Folder"), f.get("id")))
            walk(f.get("id"), depth + 1)

    walk(None, 0)
    # Folders whose parent is missing (orphans) — surface at the root level.
    for f in folders or []:
        pid = f.get("parentId")
        if pid is not None and pid not in known:
            out.append((f.get("title") or "Folder", f.get("id")))
    return out


_MAX_FEEDBACK_FILE = 5 * 1024 * 1024  # 5 MB

_FEEDBACK_CATEGORIES = [
    ("Bug — something is broken", "bug"),
    ("Feature request", "feature"),
    ("Improvement / idea", "improvement"),
    ("Question", "question"),
    ("General", "general"),
]


class FeedbackDialog(QDialog):
    """Send a bug report / feature request to the geoi team.

    Category, description (required), optional title, optional contact email
    (prefilled when signed in) and an optional screenshot/PDF (<= 5 MB). The
    auto-collected system info is shown read-only so the user sees exactly
    what is sent. Posts to /platform/feedback.
    """

    def __init__(self, system_info=None, default_email="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("geoi — send feedback")
        self.setMinimumWidth(440)
        self._attachment = None

        form = QFormLayout(self)

        self._category = QComboBox()
        for label, value in _FEEDBACK_CATEGORIES:
            self._category.addItem(label, value)
        form.addRow("Category", self._category)

        self._title = QLineEdit()
        self._title.setPlaceholderText("Short summary (optional)")
        form.addRow("Title", self._title)

        self._body = QPlainTextEdit()
        self._body.setPlaceholderText("What happened, or what would help you?")
        self._body.setMinimumHeight(120)
        form.addRow("Description", self._body)

        self._email = QLineEdit(default_email or "")
        self._email.setPlaceholderText("you@example.com (optional — for a reply)")
        form.addRow("Your email", self._email)

        attach_row = QVBoxLayout()
        self._attach_btn = QPushButton("Attach screenshot / PDF…")
        self._attach_btn.clicked.connect(self._pick_file)
        attach_row.addWidget(self._attach_btn)
        self._attach_label = QLabel("No file attached (max 5 MB)")
        self._attach_label.setWordWrap(True)
        attach_row.addWidget(self._attach_label)
        form.addRow("Attachment", attach_row)

        info = QPlainTextEdit()
        info.setReadOnly(True)
        info.setMaximumHeight(110)
        info.setPlainText(self._format_info(system_info or {}))
        form.addRow("System info (sent)", info)

        buttons = QDialogButtonBox(_SB.Ok | _SB.Cancel)
        buttons.button(_SB.Ok).setText("Send")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _format_info(info):
        return "\n".join("%s: %s" % (k, info[k]) for k in sorted(info))

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach screenshot or PDF", "",
            "Images and PDF (*.png *.jpg *.jpeg *.gif *.webp *.pdf)")
        if not path:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size <= 0 or size > _MAX_FEEDBACK_FILE:
            QMessageBox.warning(self, "geoi",
                                "Please attach an image or PDF up to 5 MB.")
            return
        self._attachment = path
        self._attach_label.setText(os.path.basename(path))

    def _on_accept(self):
        if not self.description():
            QMessageBox.warning(self, "geoi", "Please describe the issue or idea first.")
            self._body.setFocus()
            return
        self.accept()

    def category(self):
        return self._category.currentData() or "general"

    def title(self):
        return self._title.text().strip()

    def description(self):
        return self._body.toPlainText().strip()

    def email(self):
        return self._email.text().strip()

    def attachment_path(self):
        return self._attachment
