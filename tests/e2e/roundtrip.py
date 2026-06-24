"""Headless-QGIS editing round-trip against a live geoi platform.

Proves the data-loss fix end-to-end: a service is added in its OWN spatial
reference (EPSG:3857, via the plugin's own ``epsg_from_spatial_reference``),
a feature is moved, the edit is committed through QGIS's ArcGIS Feature
Service provider, and the change is then read back STRAIGHT FROM THE SERVER
and asserted to have persisted at the right coordinates.

With the old hardcoded ``crs='EPSG:4326'`` this round-trip lands the edit
off-map (degrees written as metres) — exactly the bug users hit. The script
hard-fails if a committed edit does not persist, and only skips the write
assertions on QGIS builds whose provider is genuinely read-only.

Driven by ``run.sh`` (seeds + serves the platform, then runs this). Reads
``GEOI_E2E_BASE`` and ``GEOI_E2E_SLUG`` from the environment.
"""

import json
import os
import sys
import urllib.request

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsGeometry,
    QgsPointXY,
    QgsVectorDataProvider,
    QgsVectorLayer,
)

BASE = os.environ.get("GEOI_E2E_BASE", "http://127.0.0.1:8631/platform")
SLUG = os.environ.get("GEOI_E2E_SLUG", "edittest")
TOLERANCE_M = 2.0  # 3857->4326->3857 storage round-trip is sub-metre

sys.path.insert(0, os.getcwd())
from geoi.geoi_client import epsg_from_spatial_reference  # noqa: E402


def get_json(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def _change_geometries_flag():
    try:
        return QgsVectorDataProvider.ChangeGeometries
    except AttributeError:  # PyQt6 scoped enum
        return QgsVectorDataProvider.Capability.ChangeGeometries


def main():
    app = QgsApplication([], False)
    app.initQgis()
    try:
        ver = Qgis.QGIS_VERSION
        fs = "{}/rest/services/{}/FeatureServer".format(BASE, SLUG)
        meta = get_json(fs + "?f=json")

        # The plugin's own SR logic must pick the service's real CRS.
        crs = epsg_from_spatial_reference(meta.get("spatialReference"))
        if crs != "EPSG:3857":
            fail("service SR should map to EPSG:3857, got {}".format(crs))

        uri = "crs='{}' url='{}/0'".format(crs, fs)
        layer = QgsVectorLayer(uri, "edit", "arcgisfeatureserver")
        if not layer.isValid():
            fail("layer invalid for uri {}".format(uri))

        feats = list(layer.getFeatures())
        if len(feats) != 1:
            fail("expected 1 feature on read, got {}".format(len(feats)))
        feat = feats[0]
        start = feat.geometry().asPoint()
        print("READ OK — QGIS {} — feature at 3857 ({:.2f}, {:.2f})".format(
            ver, start.x(), start.y()))

        provider = layer.dataProvider()
        if not (provider.capabilities() & _change_geometries_flag()):
            print("SKIP — arcgisfeatureserver is read-only in QGIS {} (caps: {}); "
                  "write round-trip not asserted".format(
                      ver, provider.capabilitiesString()))
            return 0

        target = QgsPointXY(start.x() + 1000.0, start.y() + 2000.0)
        if not layer.startEditing():
            fail("startEditing() returned False")
        if not layer.changeGeometry(feat.id(), QgsGeometry.fromPointXY(target)):
            fail("changeGeometry() returned False")
        if not layer.commitChanges():
            fail("commitChanges() failed: {}".format("; ".join(layer.commitErrors())))

        # Read the truth back from the server, not from QGIS's cache.
        query = (fs + "/0/query?f=json&where=1%3D1&outFields=*"
                 "&outSR=3857&returnGeometry=true")
        result = get_json(query)
        geom = result["features"][0]["geometry"]
        dx, dy = abs(geom["x"] - target.x()), abs(geom["y"] - target.y())
        if dx > TOLERANCE_M or dy > TOLERANCE_M:
            fail("edit did NOT persist — server has ({:.2f}, {:.2f}), "
                 "expected ({:.2f}, {:.2f})".format(
                     geom["x"], geom["y"], target.x(), target.y()))
        print("WRITE ROUND-TRIP OK — committed edit persisted on the server "
              "(Δ {:.3f}m, {:.3f}m)".format(dx, dy))
        return 0
    finally:
        app.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
