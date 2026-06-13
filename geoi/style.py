"""Translate QGIS symbology + labeling into the geoi symbology model.

The geoi symbology model is exactly what the platform's Web Map converter
(`src/platform/lib/WebMap.php`) and the web app consume, so a project
published or stored from QGIS renders the same in the geoi web app — and,
once the service path carries it, to ArcGIS clients too.

geoi symbology (per ``layer.symbology``):
  single        {type:'single', defaultSymbol:<symbol>, label?}
  categorical   {type:'categorical', field, stops:[<symbol>+{value,label}],
                 defaultSymbol:<symbol>}
  class-breaks  {type:'class-breaks', field,
                 classBreaks:[<symbol>+{minValue,maxValue,label}],
                 defaultSymbol:<symbol>}
  + optional symbology.labels   {enabled, field, color, haloColor,
                                 fontSize, fontFamily}
  + optional symbology.opacity  (0..1)

geoi symbol (CSS-flavoured), per geometry — matching WebMap::esriSymbolFor /
geoiSymbolFromEsri:
  Point   {color, size, outlineColor, outlineWidth}
  Line    {color, weight}
  Polygon {color, outlineColor, weight}

The pure builders + ``symbology_from_renderer`` / ``labels_from_settings``
import nothing from QGIS (they only duck-type the renderer/symbol objects),
so they are unit-tested off a QGIS install. ``layer_symbology`` is the
QGIS-facing entry the converter calls.
"""

DEFAULT_COLORS = {
    "Point": "#ef4444",
    "LineString": "#3b82f6",
    "Polygon": "#10b981",
}
FALLBACK_COLOR = "#3388ff"


def _hex(qcolor):
    """A QColor -> '#rrggbb' (alpha dropped — opacity rides on the layer)."""
    return "#{:02x}{:02x}{:02x}".format(qcolor.red(), qcolor.green(), qcolor.blue())


def _num(value, ndigits=2):
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- pure builders
def default_symbol(geom_name):
    return {"color": DEFAULT_COLORS.get(geom_name, FALLBACK_COLOR), "weight": 1}


def single_symbology(symbol, label=""):
    out = {"type": "single", "defaultSymbol": symbol}
    if label:
        out["label"] = label
    return out


def categorical_symbology(field, stops, default):
    return {
        "type": "categorical",
        "field": field,
        "stops": stops,
        "defaultSymbol": default,
    }


def class_breaks_symbology(field, breaks, default):
    return {
        "type": "class-breaks",
        "field": field,
        "classBreaks": breaks,
        "defaultSymbol": default,
    }


# --------------------------------------------------------- QGIS symbol -> geoi
def _stroke(symbol):
    """(outlineColorHex, outlineWidth) from a symbol's first layer, or (None, None)."""
    try:
        sl = symbol.symbolLayer(0)
    except Exception:  # noqa: BLE001
        return None, None
    color = None
    width = None
    try:
        if hasattr(sl, "strokeColor"):
            color = _hex(sl.strokeColor())
    except Exception:  # noqa: BLE001
        color = None
    try:
        if hasattr(sl, "strokeWidth"):
            width = _num(sl.strokeWidth())
    except Exception:  # noqa: BLE001
        width = None
    return color, width


def symbol_to_geoi(symbol, geom_name):
    """One QGIS symbol -> a geoi (CSS-flavoured) symbol dict (best-effort)."""
    out = {}
    try:
        out["color"] = _hex(symbol.color())
    except Exception:  # noqa: BLE001
        out["color"] = DEFAULT_COLORS.get(geom_name, FALLBACK_COLOR)

    if geom_name == "Point":
        size = None
        try:
            if hasattr(symbol, "size"):
                size = _num(symbol.size())
        except Exception:  # noqa: BLE001
            size = None
        if size:
            out["size"] = size
        color, width = _stroke(symbol)
        if color:
            out["outlineColor"] = color
        if width is not None:
            out["outlineWidth"] = width
    elif geom_name == "LineString":
        try:
            if hasattr(symbol, "width"):
                w = _num(symbol.width())
                if w is not None:
                    out["weight"] = w
        except Exception:  # noqa: BLE001
            pass
    else:  # Polygon — colour is the fill; outline carries the stroke
        color, width = _stroke(symbol)
        if color:
            out["outlineColor"] = color
        if width is not None:
            out["weight"] = width
    return out


def _category_value(value):
    if isinstance(value, (str, int, float, bool)):
        return value
    if value is None:
        return ""
    return str(value)


def symbology_from_renderer(renderer, geom_name):
    """Map a QGIS renderer to a geoi symbology dict (best-effort).

    Handles single-symbol -> single, categorized -> categorical (stops),
    graduated -> class-breaks. Anything else degrades to a single default.
    """
    fallback = single_symbology(default_symbol(geom_name))
    if renderer is None:
        return fallback
    rtype = renderer.type() if hasattr(renderer, "type") else ""
    try:
        if rtype == "singleSymbol":
            return single_symbology(symbol_to_geoi(renderer.symbol(), geom_name))

        if rtype == "categorizedSymbol":
            field = renderer.classAttribute()
            stops, default = [], None
            for cat in renderer.categories():
                symbol = symbol_to_geoi(cat.symbol(), geom_name)
                value = cat.value()
                if value is None or value == "":
                    default = symbol  # the "all other values" catch-all
                    continue
                entry = dict(symbol)
                entry["value"] = _category_value(value)
                label = cat.label() if hasattr(cat, "label") else ""
                if label:
                    entry["label"] = label
                stops.append(entry)
            if stops:
                return categorical_symbology(
                    field, stops, default or default_symbol(geom_name))

        if rtype == "graduatedSymbol":
            field = renderer.classAttribute()
            breaks = []
            for rng in renderer.ranges():
                entry = dict(symbol_to_geoi(rng.symbol(), geom_name))
                entry["minValue"] = _num(rng.lowerValue(), 6)
                entry["maxValue"] = _num(rng.upperValue(), 6)
                label = rng.label() if hasattr(rng, "label") else ""
                if label:
                    entry["label"] = label
                breaks.append(entry)
            if breaks:
                return class_breaks_symbology(
                    field, breaks, default_symbol(geom_name))
    except Exception:  # noqa: BLE001 - styling must never block a publish
        return fallback
    return fallback


def labels_from_settings(settings, text_format):
    """Build a geoi labels block from QGIS label settings + text format.

    ``settings`` exposes ``fieldName``; ``text_format`` is a QgsTextFormat
    (``size()``, ``font()``, ``color()``, ``buffer()``). Returns None when
    there is no field to label by.
    """
    field = getattr(settings, "fieldName", "") or ""
    if not field:
        return None
    out = {"enabled": True, "field": field}
    try:
        size = _num(text_format.size(), 1)
        if size:
            out["fontSize"] = size
    except Exception:  # noqa: BLE001
        pass
    try:
        out["fontFamily"] = text_format.font().family()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["color"] = _hex(text_format.color())
    except Exception:  # noqa: BLE001
        pass
    try:
        buf = text_format.buffer()
        if buf.enabled():
            out["haloColor"] = _hex(buf.color())
    except Exception:  # noqa: BLE001
        pass
    return out


_TYPE_LABELS = {
    "single": "Single symbol",
    "categorical": "Categorized",
    "class-breaks": "Graduated",
}


def describe(symbology, feature_count=None):
    """A short human summary of a layer's symbology for the publish dialog,
    e.g. "Categorized · kind · labels  (1,240 features)"."""
    sym = symbology or {}
    text = _TYPE_LABELS.get(sym.get("type", "single"), "Single symbol")
    field = sym.get("field")
    if field and sym.get("type") in ("categorical", "class-breaks"):
        text += " · " + str(field)
    if (sym.get("labels") or {}).get("enabled"):
        text += " · labels"
    if isinstance(feature_count, int) and feature_count >= 0:
        text += "  ({:,} feature{})".format(
            feature_count, "" if feature_count == 1 else "s")
    return text


# ------------------------------------------------------------- QGIS layer entry
def labels_from_layer(layer):
    """Extract a geoi labels block from a QGIS layer, or None."""
    try:
        if not layer.labelsEnabled():
            return None
        labeling = layer.labeling()
        if labeling is None or not hasattr(labeling, "settings"):
            return None
        settings = labeling.settings()
        return labels_from_settings(settings, settings.format())
    except Exception:  # noqa: BLE001
        return None


def layer_symbology(layer, geom_name):
    """The full geoi symbology for a QGIS layer: renderer + labels + opacity."""
    sym = symbology_from_renderer(layer.renderer(), geom_name)
    labels = labels_from_layer(layer)
    if labels:
        sym["labels"] = labels
    try:
        opacity = float(layer.opacity())
        if 0.0 <= opacity < 1.0:
            sym["opacity"] = round(opacity, 3)
    except Exception:  # noqa: BLE001
        pass
    return sym
