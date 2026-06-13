"""Unit tests for the QGIS-symbology -> geoi-symbology translator.

The renderer/symbol/labeling objects are duck-typed, so these run without a
QGIS install — lightweight fakes stand in for QgsRenderer / QgsSymbol /
QColor / QgsTextFormat. The shapes asserted here are the exact contract the
platform's WebMap converter (esriSymbolFor / rendererFor / labelingInfoFor)
consumes, so a green test means a styled QGIS layer round-trips to the web app.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import style  # noqa: E402


class FakeColor:
    def __init__(self, r, g, b, a=255):
        self._r, self._g, self._b, self._a = r, g, b, a

    def red(self):
        return self._r

    def green(self):
        return self._g

    def blue(self):
        return self._b


class FakeStrokeLayer:
    def __init__(self, color, width):
        self._c, self._w = color, width

    def strokeColor(self):
        return self._c

    def strokeWidth(self):
        return self._w


class FakeSymbol:
    def __init__(self, color, size=None, width=None, stroke=None):
        self._color = color
        self._size = size
        self._width = width
        self._stroke = stroke

    def color(self):
        return self._color

    def size(self):
        return self._size

    def width(self):
        return self._width

    def symbolLayer(self, _i):
        if self._stroke is None:
            raise IndexError
        return self._stroke


class FakeCategory:
    def __init__(self, value, symbol, label=""):
        self._v, self._s, self._l = value, symbol, label

    def value(self):
        return self._v

    def symbol(self):
        return self._s

    def label(self):
        return self._l


class FakeRange:
    def __init__(self, lo, hi, symbol, label=""):
        self._lo, self._hi, self._s, self._l = lo, hi, symbol, label

    def lowerValue(self):
        return self._lo

    def upperValue(self):
        return self._hi

    def symbol(self):
        return self._s

    def label(self):
        return self._l


class FakeRenderer:
    def __init__(self, rtype, **kw):
        self._type = rtype
        self._kw = kw

    def type(self):
        return self._type

    def symbol(self):
        return self._kw.get("symbol")

    def classAttribute(self):
        return self._kw.get("field", "")

    def categories(self):
        return self._kw.get("categories", [])

    def ranges(self):
        return self._kw.get("ranges", [])


class SymbolTest(unittest.TestCase):
    def test_point_symbol_with_size_and_outline(self):
        sym = FakeSymbol(FakeColor(255, 0, 0), size=6,
                         stroke=FakeStrokeLayer(FakeColor(0, 0, 0), 1.5))
        out = style.symbol_to_geoi(sym, "Point")
        self.assertEqual(out["color"], "#ff0000")
        self.assertEqual(out["size"], 6)
        self.assertEqual(out["outlineColor"], "#000000")
        self.assertEqual(out["outlineWidth"], 1.5)

    def test_line_symbol_weight(self):
        out = style.symbol_to_geoi(FakeSymbol(FakeColor(0, 0, 255), width=3), "LineString")
        self.assertEqual(out, {"color": "#0000ff", "weight": 3})

    def test_polygon_symbol_fill_and_outline(self):
        sym = FakeSymbol(FakeColor(16, 185, 129),
                         stroke=FakeStrokeLayer(FakeColor(5, 90, 60), 2))
        out = style.symbol_to_geoi(sym, "Polygon")
        self.assertEqual(out["color"], "#10b981")
        self.assertEqual(out["outlineColor"], "#055a3c")
        self.assertEqual(out["weight"], 2)


class RendererTest(unittest.TestCase):
    def test_single(self):
        r = FakeRenderer("singleSymbol", symbol=FakeSymbol(FakeColor(1, 2, 3), width=2))
        sym = style.symbology_from_renderer(r, "LineString")
        self.assertEqual(sym["type"], "single")
        self.assertEqual(sym["defaultSymbol"]["color"], "#010203")

    def test_categorical_with_stops_and_default(self):
        cats = [
            FakeCategory("A", FakeSymbol(FakeColor(255, 0, 0), width=1), "Type A"),
            FakeCategory("B", FakeSymbol(FakeColor(0, 255, 0), width=1)),
            FakeCategory("", FakeSymbol(FakeColor(200, 200, 200), width=1)),  # catch-all
        ]
        sym = style.symbology_from_renderer(
            FakeRenderer("categorizedSymbol", field="kind", categories=cats), "LineString")
        self.assertEqual(sym["type"], "categorical")
        self.assertEqual(sym["field"], "kind")
        self.assertEqual(len(sym["stops"]), 2)
        self.assertEqual(sym["stops"][0]["value"], "A")
        self.assertEqual(sym["stops"][0]["label"], "Type A")
        self.assertEqual(sym["stops"][0]["color"], "#ff0000")
        self.assertEqual(sym["defaultSymbol"]["color"], "#c8c8c8")

    def test_graduated_to_class_breaks(self):
        ranges = [
            FakeRange(0, 10, FakeSymbol(FakeColor(255, 255, 255), width=1), "0–10"),
            FakeRange(10, 20, FakeSymbol(FakeColor(0, 0, 0), width=1), "10–20"),
        ]
        sym = style.symbology_from_renderer(
            FakeRenderer("graduatedSymbol", field="pop", ranges=ranges), "Polygon")
        self.assertEqual(sym["type"], "class-breaks")
        self.assertEqual(sym["field"], "pop")
        self.assertEqual(sym["classBreaks"][0]["minValue"], 0)
        self.assertEqual(sym["classBreaks"][0]["maxValue"], 10)
        self.assertEqual(sym["classBreaks"][1]["label"], "10–20")

    def test_unknown_renderer_falls_back_to_single(self):
        sym = style.symbology_from_renderer(FakeRenderer("rule"), "Point")
        self.assertEqual(sym["type"], "single")
        self.assertIn("defaultSymbol", sym)

    def test_none_renderer(self):
        self.assertEqual(style.symbology_from_renderer(None, "Point")["type"], "single")


class FakeBuffer:
    def __init__(self, enabled, color):
        self._e, self._c = enabled, color

    def enabled(self):
        return self._e

    def color(self):
        return self._c


class FakeFont:
    def family(self):
        return "Noto Sans"


class FakeTextFormat:
    def __init__(self):
        self._buffer = FakeBuffer(True, FakeColor(255, 255, 255))

    def size(self):
        return 12.0

    def font(self):
        return FakeFont()

    def color(self):
        return FakeColor(30, 41, 59)

    def buffer(self):
        return self._buffer


class FakeSettings:
    def __init__(self, field):
        self.fieldName = field
        self._fmt = FakeTextFormat()

    def format(self):
        return self._fmt


class DescribeTest(unittest.TestCase):
    def test_single(self):
        self.assertEqual(style.describe({"type": "single"}), "Single symbol")

    def test_categorical_with_field_and_labels(self):
        sym = {"type": "categorical", "field": "kind",
               "labels": {"enabled": True, "field": "kind"}}
        self.assertEqual(style.describe(sym), "Categorized · kind · labels")

    def test_graduated_with_count(self):
        sym = {"type": "class-breaks", "field": "pop"}
        self.assertEqual(style.describe(sym, 1240), "Graduated · pop  (1,240 features)")

    def test_singular_feature_count(self):
        self.assertEqual(style.describe({"type": "single"}, 1),
                         "Single symbol  (1 feature)")


class LabelsTest(unittest.TestCase):
    def test_labels_from_settings(self):
        out = style.labels_from_settings(FakeSettings("name"), FakeTextFormat())
        self.assertEqual(out["enabled"], True)
        self.assertEqual(out["field"], "name")
        self.assertEqual(out["fontSize"], 12.0)
        self.assertEqual(out["fontFamily"], "Noto Sans")
        self.assertEqual(out["color"], "#1e293b")
        self.assertEqual(out["haloColor"], "#ffffff")

    def test_no_field_returns_none(self):
        self.assertIsNone(style.labels_from_settings(FakeSettings(""), FakeTextFormat()))


if __name__ == "__main__":
    unittest.main()
