"""Small modal dialogs: server settings, publish, save-to-geoi."""

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
            "The geoi platform to sign in to. Sign-in uses your platform's own "
            "Google sign-in — no other configuration is needed."
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
        self.setWindowTitle("Publish to geoi as a service")
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
