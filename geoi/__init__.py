"""geoi QGIS plugin package.

`classFactory` is the QGIS plugin entry point. The heavy (qgis-dependent)
imports are deferred to call time so importing this package off a QGIS
install (e.g. in unit tests) stays cheap and side-effect free.
"""


def classFactory(iface):  # noqa: N802 - QGIS-required name
    from .plugin import GeoiPlugin

    return GeoiPlugin(iface)
