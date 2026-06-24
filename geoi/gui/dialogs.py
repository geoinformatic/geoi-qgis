"""Small modal dialogs: server settings, publish, save-to-geoi."""

import os

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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

_SB = QDialogButtonBox.StandardButton


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
