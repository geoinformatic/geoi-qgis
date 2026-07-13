"""Publish 3D Tiles to geoi — a PREPARED tileset ZIP, or a POINT CLOUD
(.las / .laz / .ply) tiled IN THE PLUGIN.

Two lanes, one endpoint (``POST /tiles3d/create``):

* ``publish`` — an already-prepared tileset ZIP (geoi app export, py3dtiles,
  any tiler): validated client-side (fail FAST, before any upload) and
  uploaded as-is.
* ``publish_point_cloud`` — a raw point cloud: decoded (``pointcloud``),
  tiled into an OGC 3D Tiles 1.1 tileset (``tiles3d_encoder`` — the faithful
  pure-Python port of the web encoder), zipped in memory and uploaded. A
  georeferenced cloud also sends its WGS84 ``bounds``.

Storage, quotas and management are identical to the app path (same endpoint);
both lanes share ``raster.publish``'s shape and error discipline.

Pure / stdlib-only (``zipfile`` + the pure sibling modules) so it is
unit-testable off a QGIS install — no QGIS imports; the GUI wiring lives in
``plugin.py`` / ``tasks.py``.
"""

import inspect
import io
import json
import os
import struct
import zipfile

from . import pointcloud, tiles3d_encoder
from .geoi_client import GeoiError, tiles3d_friendly_error

# The root tileset entry point the platform serves the service from, and the
# content extensions a geoi 3D Tiles ZIP may carry (matches the server's
# allow-list in api/tiles3d.php). Informational: the SERVER is the authority;
# these only drive the fail-fast pre-flight + a friendly summary.
ROOT_TILESET = "tileset.json"
TILE_EXT = ("glb", "pnts")

# Point-cloud formats the in-plugin tiler accepts (extension sniff only — the
# decode itself goes by the BYTES).
POINT_CLOUD_EXT = ("las", "laz", "ply")


class Tiles3dError(Exception):
    """An actionable, user-facing 3D-Tiles publish error (bad ZIP or a mapped
    server rejection). ``str(exc)`` is safe to show verbatim in a dialog."""


def default_title(zip_path):
    """The default service title: the ZIP's file stem (``scan.zip`` ->
    ``scan``). A blank/None path yields ``"3D tiles"``."""
    stem = os.path.splitext(os.path.basename(zip_path or ""))[0].strip()
    return stem or "3D tiles"


def validate_tileset_zip(zip_path):
    """Fail FAST unless ``zip_path`` is a ZIP with a ROOT-level ``tileset.json``.

    A nested-only ``sub/tileset.json`` is NOT a valid root tileset and is
    rejected with a distinct, actionable message: the platform serves the
    tileset from ``<prefix>/tileset.json`` at the prefix root, so a ZIP whose
    only tileset.json is inside a sub-folder would publish a dead service.

    Returns a summary ``{tileCount, entries}`` on success; raises
    ``Tiles3dError`` (never a bare ``zipfile`` error) otherwise.
    """
    if not zip_path or not os.path.exists(zip_path):
        raise Tiles3dError("Choose a 3D Tiles ZIP file to publish.")
    if not zipfile.is_zipfile(zip_path):
        raise Tiles3dError(
            "That file is not a ZIP archive. Publish a 3D Tiles tileset ZIP "
            "(root tileset.json plus its .glb / .pnts tiles)."
        )
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
    except zipfile.BadZipFile as exc:
        raise Tiles3dError("The ZIP archive is corrupt: {}".format(exc))

    if not names:
        raise Tiles3dError("The ZIP archive is empty.")

    # Normalise Windows separators before the root check. A ROOT tileset.json
    # has no path segment; anything with a slash is nested.
    norm = [n.replace("\\", "/") for n in names]
    if ROOT_TILESET not in norm:
        nested = any(os.path.basename(n) == ROOT_TILESET for n in norm)
        if nested:
            raise Tiles3dError(
                "tileset.json is not at the ROOT of the ZIP. Re-zip so "
                "tileset.json sits at the top level, not inside a sub-folder."
            )
        raise Tiles3dError(
            "The ZIP has no tileset.json. A 3D Tiles tileset needs a root "
            "tileset.json plus its .glb / .pnts tiles."
        )

    tile_count = sum(
        1 for n in norm
        if os.path.splitext(n)[1].lower().lstrip(".") in TILE_EXT
    )
    return {"tileCount": tile_count, "entries": len(names)}


def publish(client, zip_path, title="", is_cancelled=None):
    """Validate + upload a prepared tileset ZIP via ``POST /tiles3d/create``.

    Returns ``{id, title, tilesetUrl}``. Raises ``Tiles3dError`` for a bad ZIP
    (BEFORE any upload) and surfaces a server ``GeoiError`` as a friendly
    ``Tiles3dError`` (quota full / not enabled / too large / …). ``is_cancelled``
    is polled before the upload starts so a cancel never posts.
    """
    validate_tileset_zip(zip_path)
    if is_cancelled and is_cancelled():
        raise Tiles3dError("Publishing cancelled.")
    title = (title or default_title(zip_path)).strip() or default_title(zip_path)
    try:
        svc = client.tiles3d_create(zip_path, title)
    except GeoiError as exc:
        # Map the stable server error CODE to a clear sentence.
        raise Tiles3dError(tiles3d_friendly_error(exc))
    urls = svc.get("urls") if isinstance(svc.get("urls"), dict) else {}
    tileset_url = client.tiles3d_tileset_url(urls.get("tileset"))
    return {
        "id": svc.get("id"),
        "title": svc.get("title") or title,
        "tilesetUrl": tileset_url,
    }


# ======================================================== point-cloud lane
def looks_like_point_cloud(path):
    """Extension sniff: True for a ``.las`` / ``.laz`` / ``.ply`` path (the
    decode itself goes by the bytes; this only routes the UI)."""
    ext = os.path.splitext(str(path or ""))[1].lower().lstrip(".")
    return ext in POINT_CLOUD_EXT


def _create_accepts_bounds(client):
    """True when ``client.tiles3d_create`` can take a ``bounds`` kwarg (a
    newer client, or one accepting **kwargs). Older clients still publish —
    just without the registry bounds metadata (fail-soft, never a crash)."""
    try:
        params = inspect.signature(client.tiles3d_create).parameters
    except (TypeError, ValueError):
        return False
    if "bounds" in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD
               for p in params.values())


def _zip_tileset(files):
    """Deterministic in-memory ZIP of the built tileset files (fixed DOS
    epoch timestamps — no wall-clock, mirroring the encoder's determinism)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in files:
            info = zipfile.ZipInfo(f["path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, f["bytes"],
                        compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def publish_point_cloud(client, path, title="", placement="reproject",
                        reproject_fn=None, progress=None, is_cancelled=None):
    """Tile a point-cloud file into an OGC 3D Tiles tileset and publish it.

    read file -> ``pointcloud.to_cloud`` -> ``tiles3d_encoder.build_tileset``
    -> in-memory ZIP (root tileset.json + tiles) -> ``client.tiles3d_create``
    (the same single multipart POST the prepared-ZIP lane uses). WGS84
    ``bounds`` ride along ONLY when the cloud is georeferenced (and the
    client supports the kwarg). ``placement`` / ``reproject_fn`` are passed
    through to :func:`pointcloud.to_cloud` — ``'local'`` centres on the
    centroid (works for any CRS), ``reproject_fn(x, y) -> (lat, lng)`` lets
    the GUI inject a QgsCoordinateTransform-backed reprojection.

    ``progress`` (0..100) is staged read 0-10 / tile 10-85 / zip 85-90 /
    upload 90-100; ``is_cancelled`` is polled at every stage boundary.
    Returns ``{id, title, tilesetUrl, tileCount, pointCount, georeferenced}``;
    raises ``Tiles3dError`` with actionable text on any failure."""
    def _p(v):
        if progress:
            progress(v)

    def _guard():
        if is_cancelled and is_cancelled():
            raise Tiles3dError("Publishing cancelled.")

    if not path or not os.path.exists(path):
        raise Tiles3dError(
            "Choose a point cloud file (.las, .laz or .ply) to publish.")
    _p(0)
    _guard()
    with open(path, "rb") as fh:
        data = fh.read()
    _p(5)
    _guard()

    try:
        result = pointcloud.to_cloud(data, os.path.basename(path),
                                     placement=placement,
                                     reproject_fn=reproject_fn)
    except pointcloud.PointCloudError as exc:
        raise Tiles3dError(str(exc))
    _p(10)
    _guard()

    opts = {}
    if result.get("origin"):
        opts["origin"] = result["origin"]
    try:
        built = tiles3d_encoder.build_tileset(result["cloud"], opts)
    except tiles3d_encoder.TilesetEncodeError as exc:
        raise Tiles3dError("Could not build the 3D tileset: {}".format(exc))
    _p(85)
    _guard()

    zip_bytes = _zip_tileset(built["files"])
    _p(90)
    _guard()

    stem = os.path.splitext(os.path.basename(path))[0].strip()
    title = (title or stem).strip() or "3D tiles"
    kwargs = {"filename": (stem or "tileset") + ".zip"}
    bounds = result.get("bounds")
    if bounds and _create_accepts_bounds(client):
        kwargs["bounds"] = bounds
    try:
        svc = client.tiles3d_create(zip_bytes, title, **kwargs)
    except GeoiError as exc:
        raise Tiles3dError(tiles3d_friendly_error(exc))
    _p(100)

    urls = svc.get("urls") if isinstance(svc.get("urls"), dict) else {}
    return {
        "id": svc.get("id"),
        "title": svc.get("title") or title,
        "tilesetUrl": client.tiles3d_tileset_url(urls.get("tileset")),
        "tileCount": built["tile_count"],
        "pointCount": result["cloud"]["point_count"],
        "georeferenced": bool(result.get("origin")),
    }


# ================================================== point-cloud tileset sniff
# QGIS's native ``cesiumtiles`` provider (``QgsTiledSceneLayer``) renders only
# MESH tilesets — a point-primitive tileset loads "valid" but shows nothing
# ("Point objects in tiled scenes are not supported"). geoi's own point-cloud
# encoder emits ``.glb`` content with glTF ``"mode": 0`` (POINTS), NOT
# ``.pnts``, so detection inspects the ACTUAL glTF primitive mode — not just
# the extension — while still honouring the legacy ``.pnts`` a foreign tiler
# may use. All pure / stdlib so the add-path guard is unit-testable off QGIS.
def content_uris(tileset_dict):
    """Ordered content uri strings at ``root.content.uri`` and each
    ``root.children[*].content.uri`` — root plus ONE level of children only
    (bounded, no unbounded recursion). Any missing / malformed structure
    yields ``[]``."""
    if not isinstance(tileset_dict, dict):
        return []
    root = tileset_dict.get("root")
    if not isinstance(root, dict):
        return []

    def _uri(node):
        if not isinstance(node, dict):
            return None
        content = node.get("content")
        if not isinstance(content, dict):
            return None
        uri = content.get("uri")
        return uri if isinstance(uri, str) and uri else None

    out = []
    root_uri = _uri(root)
    if root_uri:
        out.append(root_uri)
    children = root.get("children")
    if isinstance(children, list):
        for child in children:
            child_uri = _uri(child)
            if child_uri:
                out.append(child_uri)
    return out


def glb_is_points(glb_bytes):
    """``True`` if a GLB's glTF has ANY ``meshes[*].primitives[*].mode == 0``
    (glTF POINTS), else ``False``.

    Every byte-offset access is bounds-checked against ``len(glb_bytes)`` — a
    truncated, garbage or too-short input returns ``False``, never raising or
    hanging. Only the JSON chunk is parsed (the binary chunk is skipped)."""
    if not isinstance(glb_bytes, (bytes, bytearray)):
        return False
    data = bytes(glb_bytes)
    # 12-byte GLB header ("glTF" + version + total length) then chunk 0:
    # 4-byte LE length + 4-byte type ("JSON") + that many bytes of JSON.
    if len(data) < 20 or data[0:4] != b"glTF":
        return False
    try:
        (chunk_len,) = struct.unpack_from("<I", data, 12)
    except struct.error:
        return False
    if data[16:20] != b"JSON":
        return False
    start, end = 20, 20 + chunk_len
    if chunk_len <= 0 or end > len(data):
        return False
    try:
        gltf = json.loads(data[start:end].decode("utf-8", "replace"))
    except ValueError:
        return False
    if not isinstance(gltf, dict):
        return False
    meshes = gltf.get("meshes")
    if not isinstance(meshes, list):
        return False
    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        primitives = mesh.get("primitives")
        if not isinstance(primitives, list):
            continue
        for prim in primitives:
            if isinstance(prim, dict) and prim.get("mode") == 0:
                return True
    return False


def tileset_is_point_cloud(tileset_dict, fetch_glb):
    """Whether a tileset is a POINT CLOUD QGIS can't render.

    ``fetch_glb(uri) -> bytes`` fetches a content GLB (injected so this stays
    testable without a network). Inspects the FIRST content uri:

    * ``.pnts`` (case-insensitive) -> ``True`` immediately (legacy point
      format), WITHOUT calling ``fetch_glb``;
    * ``.glb`` -> ``glb_is_points(fetch_glb(uri))`` — a ``fetch_glb`` error
      fails OPEN (``False``) so a real mesh tileset is never blocked on a
      transient fetch failure;
    * any other extension, or no content uri at all -> ``False`` (unknown,
      don't block)."""
    uris = content_uris(tileset_dict)
    if not uris:
        return False
    first = uris[0]
    low = first.lower()
    if low.endswith(".pnts"):
        return True
    if low.endswith(".glb"):
        try:
            glb = fetch_glb(first)
        except Exception:  # noqa: BLE001 - a fetch error must not block a mesh
            return False
        return glb_is_points(glb)
    return False
