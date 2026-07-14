"""Group MANAGEMENT (1.5.0): the ManageGroupsDialog owner-only gating and the
menu wiring that reaches it, all off a real QGIS install.

* B2 — ManageGroupsDialog disables the owner-only actions (Rename / Delete /
  Add member / Remove member) for a group the caller does NOT own, mirroring
  how browser_panel hides owner-only actions on shared items.
* B3 — the tree ROOT context menu offers "Manage groups…".
* B4 — the service context menu shows a checkable "Editable (read/write)"
  action reflecting the service's current ``editable`` flag, wired to the
  controller's ``set_service_editable``.

``qgis`` + the Qt widget modules are stubbed (no QGIS/PyQt in CI), with just
enough real behaviour — a QListWidget that tracks the current row, a
QPushButton that tracks its enabled state, and a QMenu/QAction that record
calls — to prove the gating and wiring behaviourally.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------- tiny Qt stubs
class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *a):
        for fn in list(self._slots):
            fn(*a)


class _Any:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, _n):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()


class _Role:
    UserRole = 256


class _QtStub:
    ItemDataRole = _Role


class _SBEnum:
    Close = 1
    Ok = 2
    Cancel = 4


class _Button:
    def __init__(self, *a, **k):
        self._enabled = True
        self.clicked = _Signal()

    def setEnabled(self, v):
        self._enabled = bool(v)

    def isEnabled(self):
        return self._enabled

    def __getattr__(self, _n):  # setToolTip / setIconSize / … are no-ops
        return _Any()


class _ButtonBox:
    StandardButton = _SBEnum

    def __init__(self, *a, **k):
        self.rejected = _Signal()
        self.accepted = _Signal()
        self._btn = _Button()

    def button(self, _which):
        return self._btn


class _Dialog:
    def __init__(self, *a, **k):
        pass

    def setWindowTitle(self, *a):
        pass

    def setMinimumWidth(self, *a):
        pass

    def accept(self):
        pass

    def reject(self):
        pass


class _Layout:
    def __init__(self, *a, **k):
        pass

    def addWidget(self, *a, **k):
        pass

    def addLayout(self, *a, **k):
        pass


class _Item:
    def __init__(self, text=""):
        self._text = text
        self._data = {}

    def setData(self, role, val):
        self._data[role] = val

    def data(self, role):
        return self._data.get(role)

    def text(self):
        return self._text


class _ListWidget:
    def __init__(self, *a, **k):
        self._items = []
        self._current = None
        self.currentItemChanged = _Signal()

    def clear(self):
        self._items = []
        self._current = None

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]

    def setCurrentRow(self, i):
        if 0 <= i < len(self._items):
            prev, self._current = self._current, self._items[i]
            self.currentItemChanged.emit(self._current, prev)

    def currentItem(self):
        return self._current

    def setEnabled(self, _v):
        pass


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(m, key, val)
    sys.modules[name] = m
    return m


def _import_dialogs():
    saved = {
        k: sys.modules.get(k)
        for k in ("qgis", "qgis.PyQt", "qgis.PyQt.QtCore",
                  "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets", "geoi.gui.dialogs")
    }
    _mod("qgis")
    _mod("qgis.PyQt")
    _mod("qgis.PyQt.QtCore", Qt=_QtStub, QSettings=_Any)
    _mod("qgis.PyQt.QtGui", QIcon=_Any)
    _mod("qgis.PyQt.QtWidgets",
         QCheckBox=_Any, QComboBox=_Any, QDialog=_Dialog,
         QDialogButtonBox=_ButtonBox, QFileDialog=_Any, QFormLayout=_Layout,
         QHBoxLayout=_Layout, QInputDialog=_Any, QLabel=_Any, QLineEdit=_Any,
         QListWidget=_ListWidget, QListWidgetItem=_Item, QMessageBox=_Any,
         QPlainTextEdit=_Any, QPushButton=_Button, QVBoxLayout=_Layout)

    sys.modules.pop("geoi.gui.dialogs", None)
    from geoi.gui import dialogs as dlg_mod

    def cleanup():
        sys.modules.pop("geoi.gui.dialogs", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return dlg_mod, cleanup


class _GroupClient:
    """Records calls; get_group echoes the group's own role + one member."""

    def __init__(self, groups):
        self._by_id = {g["id"]: g for g in groups}
        self.calls = []

    def get_group(self, gid):
        self.calls.append(("get_group", gid))
        g = self._by_id.get(gid, {})
        return {
            "group": {"id": gid, "myRole": g.get("myRole")},
            "members": [{"userId": 7, "email": "a@b.c", "role": "owner"}],
        }

    def list_groups(self):
        self.calls.append(("list_groups",))
        return list(self._by_id.values())


# ------------------------------------------------------------------ B2: gating
class ManageGroupsDialogGatingTest(unittest.TestCase):
    GROUPS = [
        {"id": 1, "name": "Mine", "myRole": "owner"},
        {"id": 2, "name": "Theirs", "myRole": "member"},
    ]

    def setUp(self):
        self.dlg_mod, self._cleanup = _import_dialogs()

    def tearDown(self):
        self._cleanup()

    def _dialog(self):
        client = _GroupClient(self.GROUPS)
        dlg = self.dlg_mod.ManageGroupsDialog(client, self.GROUPS)
        return dlg, client

    def _owner_only_buttons(self, dlg):
        return (dlg._rename_btn, dlg._delete_btn,
                dlg._add_member_btn, dlg._remove_member_btn)

    def test_is_owner_helper(self):
        is_owner = self.dlg_mod.ManageGroupsDialog._is_owner
        self.assertTrue(is_owner({"myRole": "owner"}))
        self.assertFalse(is_owner({"myRole": "member"}))
        self.assertFalse(is_owner({}))
        self.assertFalse(is_owner(None))

    def test_owner_group_enables_owner_only_actions(self):
        # Row 0 (Mine, owner) is current after construction.
        dlg, _client = self._dialog()
        for btn in self._owner_only_buttons(dlg):
            self.assertTrue(btn.isEnabled())
        # Create is always available (not owner-gated).
        self.assertTrue(dlg._create_btn.isEnabled())

    def test_non_owner_group_disables_owner_only_actions(self):
        dlg, _client = self._dialog()
        dlg._group_list.setCurrentRow(1)  # Theirs (member)
        for btn in self._owner_only_buttons(dlg):
            self.assertFalse(
                btn.isEnabled(),
                "owner-only action must be disabled for a non-owner group")
        # Create stays available even on a group you don't own.
        self.assertTrue(dlg._create_btn.isEnabled())

    def test_selecting_a_group_loads_its_members(self):
        dlg, client = self._dialog()
        dlg._group_list.setCurrentRow(1)
        self.assertIn(("get_group", 2), client.calls)


# --------------------------------------------- B3/B4: context-menu wiring
class _Action:
    def __init__(self, text=""):
        self.text = text
        self.slot = None
        self.checkable = False
        self.checked = False
        self.toggled = _Signal()

    def setCheckable(self, v):
        self.checkable = bool(v)

    def setChecked(self, v):
        self.checked = bool(v)


class _RecMenu:
    def __init__(self, *a, **k):
        self.actions = []

    def addAction(self, text, slot=None):
        action = _Action(text)
        action.slot = slot
        self.actions.append(action)
        return action

    def addSeparator(self):
        pass

    def exec(self, *a, **k):
        pass


class _MenuItem:
    def __init__(self, roles):
        self._roles = roles

    def data(self, _col, role):
        return self._roles.get(role)


class _StubTree:
    def __init__(self, item=None, selected=None):
        self._item = item
        self._selected = selected if selected is not None else (
            [item] if item is not None else [])

    def itemAt(self, _point):
        return self._item

    def selectedItems(self):
        return self._selected

    def viewport(self):
        return _Any()

    def editItem(self, *a):
        pass


class _RecController:
    def __init__(self):
        self.calls = []
        self._recorders = {}

    def __getattr__(self, name):
        # Cache one recorder per name so ``controller.x is controller.x`` — the
        # menu wiring stores the bound method and the test compares identity.
        recorders = self.__dict__.setdefault("_recorders", {})
        if name not in recorders:
            def rec(*a, _name=name, **k):
                self.calls.append((_name,) + a)
            recorders[name] = rec
        return recorders[name]


def _import_browser_panel():
    saved = {
        k: sys.modules.get(k)
        for k in ("qgis", "qgis.core", "qgis.gui", "qgis.PyQt",
                  "qgis.PyQt.QtCore", "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
                  "geoi.gui.browser_panel")
    }
    _mod("qgis")
    _mod("qgis.core")
    _mod("qgis.gui", QgsDockWidget=_Any)
    _mod("qgis.PyQt")
    _mod("qgis.PyQt.QtCore", QSize=_Any, Qt=_Any())
    _mod("qgis.PyQt.QtGui", QIcon=_Any)
    _mod("qgis.PyQt.QtWidgets", QAbstractItemView=_Any, QGridLayout=_Any,
         QHBoxLayout=_Any, QLabel=_Any, QMenu=_RecMenu, QPushButton=_Any,
         QSizePolicy=_Any, QStyle=_Any, QToolButton=_Any, QTreeWidget=_Any,
         QTreeWidgetItem=_Any, QVBoxLayout=_Any, QWidget=_Any)

    sys.modules.pop("geoi.gui.browser_panel", None)
    from geoi.gui import browser_panel as bp

    def cleanup():
        sys.modules.pop("geoi.gui.browser_panel", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return bp, cleanup


class ContextMenuWiringTest(unittest.TestCase):
    def setUp(self):
        self.bp, self._cleanup = _import_browser_panel()

    def tearDown(self):
        self._cleanup()

    def _panel(self, tree, controller):
        panel = self.bp.GeoiPanel.__new__(self.bp.GeoiPanel)
        panel._tree = tree
        panel._c = controller
        return panel

    def _run(self, tree, controller):
        # _on_context_menu builds `menu = QMenu(...)` internally; intercept it.
        built = {}
        orig = self.bp.QMenu

        def factory(*a, **k):
            built["menu"] = _RecMenu()
            return built["menu"]

        self.bp.QMenu = factory
        try:
            self._panel(tree, controller)._on_context_menu(object())
        finally:
            self.bp.QMenu = orig
        return built["menu"]

    def test_root_menu_offers_manage_groups(self):
        controller = _RecController()
        menu = self._run(_StubTree(item=None, selected=[]), controller)
        labels = [a.text for a in menu.actions]
        self.assertIn("Manage groups…", labels)
        self.assertIn("New folder…", labels)
        self.assertIn("Refresh", labels)
        # The action is wired straight to the controller entry point.
        action = next(a for a in menu.actions if a.text == "Manage groups…")
        self.assertEqual(action.slot, controller.manage_groups)

    def test_service_menu_shows_editable_checkbox_checked(self):
        payload = {"name": "roads", "title": "Roads", "editable": True}
        item = _MenuItem({self.bp.ROLE_KIND: "service",
                          self.bp.ROLE_PAYLOAD: payload})
        controller = _RecController()
        menu = self._run(_StubTree(item=item), controller)
        action = next(
            (a for a in menu.actions if a.text == "Editable (read/write)"),
            None)
        self.assertIsNotNone(action, "service menu must offer the toggle")
        self.assertTrue(action.checkable)
        self.assertTrue(action.checked, "reflects payload['editable'] == True")

    def test_service_menu_editable_unchecked_when_read_only(self):
        payload = {"name": "roads", "title": "Roads", "editable": False}
        item = _MenuItem({self.bp.ROLE_KIND: "service",
                          self.bp.ROLE_PAYLOAD: payload})
        menu = self._run(_StubTree(item=item), _RecController())
        action = next(a for a in menu.actions
                      if a.text == "Editable (read/write)")
        self.assertTrue(action.checkable)
        self.assertFalse(action.checked)

    def test_service_menu_editable_toggle_calls_controller(self):
        payload = {"name": "roads", "title": "Roads", "editable": False}
        item = _MenuItem({self.bp.ROLE_KIND: "service",
                          self.bp.ROLE_PAYLOAD: payload})
        controller = _RecController()
        menu = self._run(_StubTree(item=item), controller)
        action = next(a for a in menu.actions
                      if a.text == "Editable (read/write)")
        action.toggled.emit(True)  # user ticks the box
        self.assertIn(("set_service_editable", payload, True),
                      controller.calls)


if __name__ == "__main__":
    unittest.main()
