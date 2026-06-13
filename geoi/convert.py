"""
Convert QGIS vector layers <-> the geoi project / layer / symbology model.

The pure helpers (geometry-type and field-type mapping, the symbology
dict builders, and the FeatureCollection assembler) import nothing from
QGIS, so they are unit-tested off a QGIS install. The QGIS adapters at
the bottom (``qgis_layer_to_geoi`` etc.) import qgis lazily, inside the
functions, so this module imports cleanly anywhere.

geoi model (mirrors src/app/main.js):
  layer  = {id, name, type('Point'|'LineString'|'Polygon'),
            schema:[{name,type}], symbology, visible}
  symbology = {type:'single', color, weight}
            | {type:'unique', field, domain, palette, fallback}
            | {type:'continuous', field, domain:[min,max], palette, fallback}
  project = {type:'FeatureCollection', project_metadata:{...}, features:[...]}
  CRS is WGS84 / EPSG:4326 everywhere; features carry properties._layerId.
"""

import time
import uuid


# ----------------------------------------------------------------- pure maps
def geoi_geometry_name(flat_name):
    """Map a QGIS/WKB geometry-type name to a geoi geometry type."""
    n = (flat_name or "").lower()
    if "polygon" in n or "surface" in n:
        return "Polygon"
    if "line" in n or "curve" in n:
        return "LineString"
    if "point" in n:
        return "Point"
    return "Point"


def geoi_geometry_from_code(code):
    """Map a QGIS geometry-type code to a geoi geometry type.

    layer.geometryType() is 0=Point, 1=Line, 2=Polygon — stable integer values
    across QGIS 3 (QgsWkbTypes.GeometryType) and QGIS 4 (Qgis.GeometryType).
    Using the int avoids QgsWkbTypes, which moved under the Qgis namespace in 4.0.
    """
    return {0: "Point", 1: "LineString", 2: "Polygon"}.get(int(code), "Point")


def geoi_field_type(qvariant_name):
    """Map a Qt/QVariant type name to a geoi field type.

    geoi field types: text, double, longtext, barcode, image. Numeric Qt
    types become 'double'; everything else becomes 'text'.
    """
    n = (qvariant_name or "").lower()
    if n in ("int", "uint", "longlong", "ulonglong", "double", "float"):
        return "double"
    return "text"


def new_layer_id(prefix="layer_qgis"):
    return "{}_{}_{}".format(prefix, int(time.time() * 1000), uuid.uuid4().hex[:6])


def build_feature_collection(
    layers,
    features,
    project_name="QGIS project",
    project_note="",
    layer_groups=None,
    plugins=None,
):
    """Assemble the geoi project FeatureCollection (App._featureCollectionWithMeta)."""
    return {
        "type": "FeatureCollection",
        "project_metadata": {
            "projectName": project_name,
            "projectNote": project_note,
            "layers": layers,
            "layerGroups": layer_groups or [],
            "plugins": plugins or {},
        },
        "features": features,
    }


# ----------------------------------------------------------- QGIS adapters
def schema_from_layer(layer):
    """QGIS fields -> geoi schema [{name, type}]."""
    schema = []
    for field in layer.fields():
        schema.append(
            {"name": field.name(), "type": geoi_field_type(field.typeName() or "")}
        )
    return schema


def qgis_layer_to_geoi(layer, layer_id=None):
    """Convert one QGIS vector layer to (geoi_layer_dict, [geojson_features]).

    Geometry is reprojected to EPSG:4326. Each feature is GeoJSON with
    ``properties._layerId`` / ``_layerName`` set, matching the geoi model.
    """
    import json as _json

    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
    )

    layer_id = layer_id or new_layer_id()
    geom_name = geoi_geometry_from_code(layer.geometryType())
    from . import style

    layer_dict = {
        "id": layer_id,
        "name": layer.name(),
        "type": geom_name,
        "schema": schema_from_layer(layer),
        "symbology": style.layer_symbology(layer, geom_name),
        "visible": True,
    }

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = None
    if layer.crs().isValid() and layer.crs() != wgs84:
        transform = QgsCoordinateTransform(layer.crs(), wgs84, QgsProject.instance())

    field_names = [f.name() for f in layer.fields()]
    features = []
    for feat in layer.getFeatures():
        geom = feat.geometry()
        geojson_geom = None
        if geom is not None and not geom.isEmpty():
            g = geom
            if transform is not None:
                g = QgsGeometry_clone(geom)
                g.transform(transform)
            try:
                geojson_geom = _json.loads(g.asJson(7))
            except Exception:  # noqa: BLE001
                geojson_geom = None
        props = {}
        for name in field_names:
            value = feat[name]
            props[name] = _jsonable(value)
        props["_layerId"] = layer_id
        props["_layerName"] = layer.name()
        features.append(
            {"type": "Feature", "geometry": geojson_geom, "properties": props}
        )
    return layer_dict, features


def QgsGeometry_clone(geom):
    from qgis.core import QgsGeometry

    return QgsGeometry(geom)


def _jsonable(value):
    """Make a QGIS attribute value JSON-serialisable."""
    try:
        from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QVariant

        if isinstance(value, QVariant) and value.isNull():
            return None
        if isinstance(value, (QDate, QTime)):
            return value.toString("yyyy-MM-dd")
        if isinstance(value, QDateTime):
            return value.toString("yyyy-MM-ddTHH:mm:ss")
    except Exception:  # noqa: BLE001
        pass
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
