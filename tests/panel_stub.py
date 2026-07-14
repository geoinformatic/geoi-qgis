"""Recording Qt/QGIS stubs for tests that construct the REAL ``GeoiPanel``.

Not a test module (no ``test_`` prefix, so ``unittest discover`` ignores it).
It installs recording stand-ins for every Qt widget / layout / enum the panel
touches at construction, imports a FRESH ``geoi.gui.browser_panel`` bound to
them, and returns ``(bp_module, panel, cleanup)``. The stubs record just enough
to assert the action-bar grid layout (WS1) and the Discover mode/search UX
(WS3c) off a real QGIS install.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Signal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def disconnect(self, *a):
        self._slots = []

    def emit(self, *a):
        for slot in list(self._slots):
            slot(*a)


_SIGNALS = (
    "clicked", "toggled", "textChanged", "timeout",
    "customContextMenuRequested", "itemDoubleClicked", "itemChanged",
    "itemSelectionChanged", "buttonClicked", "visibilityChanged", "triggered",
)


class _Rec:
    """Permissive widget/layout stub: known display state (text / enabled /
    visible / checked) is stored, every other method is a no-op, and the common
    Qt signals are real recordable ``_Signal`` objects."""

    def __init__(self, *a, **k):
        object.__setattr__(
            self, "_text",
            next((x for x in reversed(a) if isinstance(x, str)), ""))
        object.__setattr__(self, "_enabled", True)
        object.__setattr__(self, "_visible", True)
        object.__setattr__(self, "_checked", False)
        for name in _SIGNALS:
            object.__setattr__(self, name, _Signal())

    def setText(self, text):
        self._text = text

    def text(self):
        return self._text

    def setEnabled(self, value):
        self._enabled = bool(value)

    def isEnabled(self):
        return self._enabled

    def setVisible(self, value):
        self._visible = bool(value)

    def isVisible(self):
        return self._visible

    def setChecked(self, value):
        self._checked = bool(value)

    def isChecked(self):
        return self._checked

    def style(self):
        return _Rec()

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Dock:
    """The panel's base class — a CLEAN object (NO catch-all ``__getattr__``) so
    ``GeoiPanel``'s own missing attributes (``_icon_cache``, ``_signed_in``)
    raise ``AttributeError`` and ``getattr(self, …, default)`` works normally."""

    def __init__(self, *a, **k):
        for name in _SIGNALS:
            setattr(self, name, _Signal())

    def setObjectName(self, *a):
        pass

    def setWidget(self, *a):
        pass

    def style(self):
        return _Rec()


class _Grid(_Rec):
    """Records ``addWidget(widget, row, col, rowspan, colspan)`` +
    ``setColumnStretch`` so the action-bar layout is assertable."""

    instances = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        object.__setattr__(self, "cells", [])
        object.__setattr__(self, "stretch", {})
        _Grid.instances.append(self)

    def addWidget(self, widget, row, col, rowspan=1, colspan=1):
        self.cells.append((widget, row, col, rowspan, colspan))

    def setColumnStretch(self, col, value):
        self.stretch[col] = value

    def at(self, widget):
        for (w, row, col, rs, cs) in self.cells:
            if w is widget:
                return (row, col, rs, cs)
        return None


class _Tree(_Rec):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        object.__setattr__(self, "top", [])
        object.__setattr__(self, "_selected", [])

    def clear(self):
        self.top = []

    def addTopLevelItem(self, item):
        self.top.append(item)

    def topLevelItemCount(self):
        return len(self.top)

    def topLevelItem(self, i):
        return self.top[i]

    def selectedItems(self):
        return list(self._selected)

    def header(self):
        return _Rec()

    def viewport(self):
        return _Rec()

    def editItem(self, *a):
        pass


class _Flag:
    def __and__(self, _o):
        return self

    def __or__(self, _o):
        return self

    def __invert__(self):
        return self

    def __rand__(self, _o):
        return self

    def __ror__(self, _o):
        return self


class _Enum:
    def __getattr__(self, _n):
        return _Flag()


class _EnumHolder:
    def __getattr__(self, _n):
        return _Enum()


class _Item:
    def __init__(self, cols=None):
        self._cols = list(cols) if cols else []
        self._data = {}
        self._children = []
        self._flags = _Flag()

    def setData(self, col, role, value):
        self._data[(col, role)] = value

    def data(self, col, role):
        return self._data.get((col, role))

    def setText(self, col, value):
        while len(self._cols) <= col:
            self._cols.append("")
        self._cols[col] = value

    def text(self, col=0):
        return self._cols[col] if col < len(self._cols) else ""

    def setIcon(self, *a):
        pass

    def setToolTip(self, *a):
        pass

    def addChild(self, child):
        self._children.append(child)

    def child(self, i):
        return self._children[i]

    def childCount(self):
        return len(self._children)

    def setExpanded(self, *a):
        pass

    def flags(self):
        return self._flags

    def setFlags(self, flags):
        self._flags = flags


class _Timer(_Rec):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        object.__setattr__(self, "started", 0)
        object.__setattr__(self, "stopped", 0)

    def setSingleShot(self, *a):
        pass

    def setInterval(self, *a):
        pass

    def start(self, *a):
        self.started += 1

    def stop(self, *a):
        self.stopped += 1

    def isActive(self):
        return self.started > self.stopped


class _Menu:
    def __init__(self, *a, **k):
        self.actions = []

    def addAction(self, text, slot=None):
        act = _Rec(text)
        object.__setattr__(act, "slot", slot)
        object.__setattr__(act, "checkable", False)
        object.__setattr__(act, "checked", False)
        self.actions.append(act)
        return act

    def addSeparator(self):
        self.actions.append(_Rec("__sep__"))

    def exec(self, *a, **k):
        pass


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(m, key, val)
    sys.modules[name] = m
    return m


def install():
    """Install the recording stubs and import a fresh browser_panel.

    Returns ``(bp_module, cleanup)``. ``_Grid.instances`` is reset so the test
    can find the action grid deterministically.
    """
    _Grid.instances = []
    saved = {
        k: sys.modules.get(k)
        for k in ("qgis", "qgis.core", "qgis.gui", "qgis.PyQt",
                  "qgis.PyQt.QtCore", "qgis.PyQt.QtGui", "qgis.PyQt.QtWidgets",
                  "geoi.gui.browser_panel")
    }
    _mod("qgis")
    _mod("qgis.core")
    _mod("qgis.gui", QgsDockWidget=_Dock)
    _mod("qgis.PyQt")
    _mod("qgis.PyQt.QtCore", QSize=_Rec, Qt=_EnumHolder(), QTimer=_Timer)
    _mod("qgis.PyQt.QtGui", QIcon=_Rec)
    _mod("qgis.PyQt.QtWidgets",
         QAbstractItemView=_EnumHolder(), QButtonGroup=_Rec, QGridLayout=_Grid,
         QHBoxLayout=_Rec, QLabel=_Rec, QLineEdit=_Rec, QMenu=_Menu,
         QPushButton=_Rec, QSizePolicy=_EnumHolder(), QStyle=_EnumHolder(),
         QToolButton=_Rec, QTreeWidget=_Tree, QTreeWidgetItem=_Item,
         QVBoxLayout=_Rec, QWidget=_Rec)

    # Force a genuinely FRESH browser_panel bound to THESE stubs: popping
    # sys.modules is not enough — the parent package keeps a stale
    # ``geoi.gui.browser_panel`` attribute that ``from geoi.gui import …`` would
    # return unchanged, so drop that attribute and re-import explicitly.
    import importlib
    import geoi.gui as gui_pkg
    saved_attr = getattr(gui_pkg, "browser_panel", None)
    sys.modules.pop("geoi.gui.browser_panel", None)
    if hasattr(gui_pkg, "browser_panel"):
        delattr(gui_pkg, "browser_panel")
    bp = importlib.import_module("geoi.gui.browser_panel")

    def cleanup():
        sys.modules.pop("geoi.gui.browser_panel", None)
        if saved_attr is not None:
            gui_pkg.browser_panel = saved_attr
        elif hasattr(gui_pkg, "browser_panel"):
            delattr(gui_pkg, "browser_panel")
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return bp, cleanup
