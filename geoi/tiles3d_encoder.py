"""OGC 3D Tiles 1.1 tileset builder — pure-Python port of the geoi web
encoder (``src/scan3d/tiles3d-encoder.js``).

Turns a LOCAL metric Y-up colored point cloud (the ``pointcloud.to_cloud``
output shape) into a tileset — a ``tileset.json`` + one glTF 2.0 GLB per
tile — that the platform's frozen PHP ``Tileset3DValidator`` accepts on
``POST /tiles3d/create``. OGC conformance is the requirement; the tree
layout, GLB layout, JSON key order and all constants mirror the JS encoder.

Pure / stdlib-only (``struct``/``json``/``math``) — NO QGIS or PyQt imports.
DETERMINISTIC: no wall-clock, no randomness — a given cloud + options always
yields identical bytes (CI + committed-fixture stability).

Strict-or-throw (like the JS twin): every input is validated UP FRONT — a
non-finite / out-of-range / mis-sized array raises a ``malformed``-flagged
:class:`TilesetEncodeError` before a single byte is emitted, so a bad cloud
never yields a partial tileset.

AXIS CONVENTION (from the JS twin — read before touching the math):
the cloud is scan-local metres, Y up (East=+x, North=-z, Up=+y). A tile GLB
keeps those positions VERBATIM. The 3D-Tiles TILE-FRAME is the glTF Y-up ->
Z-up rotation (a, b, c) = (x, -z, y): the boundingVolume.box and the root
``transform`` both operate there. The root transform composes the ENU->ECEF
basis at the origin with the heading rotation and that axis mapping —
column-major, translation = the WGS84->ECEF of the origin.
"""

import json
import math
import struct
from array import array

GENERATOR = "geoi"            # static (no version/time) -> deterministic
SINGLE_TILE_MAX = 50000       # <= this => one root tile
LEAF_MAX = 20000              # a node <= this becomes a leaf
LEVEL_STRIDE = 8              # internal node keeps 1/8, pushes 7/8 down
DEFAULT_MAX_DEPTH = 3

_WGS84_A = 6378137.0          # semi-major axis, metres
_WGS84_F = 1.0 / 298.257223563
_DEG = math.pi / 180.0


class TilesetEncodeError(ValueError):
    """A malformed cloud/argument (strict-or-throw, mirrors the JS
    ``RangeError`` flagged ``.malformed``)."""

    malformed = True


def _throw(msg):
    raise TilesetEncodeError("Tiles3DEncoder: " + msg)


def _fin(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _color_ok(v):
    """An integer 0..255 (a float carrying an integral value also passes,
    mirroring the JS isInt + range check)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return 0 <= v <= 255
    if isinstance(v, float):
        return math.isfinite(v) and v == math.floor(v) and 0 <= v <= 255
    return False


def _jnum(v):
    """JSON number formatting parity with ``JSON.stringify``: an integral
    float is emitted as an integer (JS prints 2.0 as ``2``)."""
    if isinstance(v, float) and v.is_integer() and abs(v) <= 2 ** 53:
        return int(v)
    return v


def _jlist(seq):
    return [_jnum(v) for v in seq]


def _js_num_str(v):
    """ECMAScript ``Number::toString(10)`` for a finite float — byte parity
    with ``JSON.stringify``. Python's ``repr`` already picks the identical
    shortest round-trip digits; only the plain-vs-exponent format thresholds
    differ (JS stays plain for 1e-6 <= |v| < 1e21, Python for a narrower
    band, and JS pads no leading zero into the exponent: ``1e-7``, not
    ``1e-07``)."""
    if v == 0:
        return "0"                       # covers -0.0 (JS stringifies to "0")
    s = repr(v)
    if "e" not in s and "E" not in s:
        return s                         # plain Python form == plain JS form
    mant, exp_txt = s.lower().split("e")
    neg = mant.startswith("-")
    if neg:
        mant = mant[1:]
    ip, _, fp = mant.partition(".")
    digits = (ip + fp).rstrip("0") or "0"
    n = len(ip) + int(exp_txt)           # value = 0.<digits> * 10^n
    k = len(digits)
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        head = digits if k == 1 else digits[0] + "." + digits[1:]
        out = head + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return ("-" + out) if neg else out


def _json_stringify(obj):
    """Minimal ``JSON.stringify`` twin (no spaces, insertion-order keys,
    JS number formatting) for the tileset/glTF documents this module builds
    — dict/list/str/int/float/bool only."""
    if isinstance(obj, dict):
        return "{" + ",".join(
            json.dumps(str(k)) + ":" + _json_stringify(x)
            for k, x in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_json_stringify(x) for x in obj) + "]"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, float):
        return _js_num_str(v=obj)
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, str):
        return json.dumps(obj)
    _throw("unsupported JSON value {!r}".format(obj))


# ---- cloud validation (strict, single up-front pass) -----------------------
def _validate_cloud(cloud):
    if not isinstance(cloud, dict):
        _throw("cloud dict required")
    n = cloud.get("point_count")
    if not _is_int(n) or n <= 0:
        _throw("cloud.point_count must be a positive integer")
    pos = cloud.get("positions")
    if pos is None or not hasattr(pos, "__len__") or len(pos) != 3 * n:
        _throw("cloud.positions length must be 3*point_count")
    col = cloud.get("colors")
    if col is None or not hasattr(col, "__len__") or len(col) != 3 * n:
        _throw("cloud.colors length must be 3*point_count")
    for i in range(n):
        p = 3 * i
        if not (_fin(pos[p]) and _fin(pos[p + 1]) and _fin(pos[p + 2])):
            _throw("position[{}] not finite".format(i))
        if not _color_ok(col[p]):
            _throw("color R[{}] must be an integer 0..255".format(i))
        if not _color_ok(col[p + 1]):
            _throw("color G[{}] must be an integer 0..255".format(i))
        if not _color_ok(col[p + 2]):
            _throw("color B[{}] must be an integer 0..255".format(i))
    return n


def _bounds(pos, n):
    mnx = mny = mnz = math.inf
    mxx = mxy = mxz = -math.inf
    for i in range(n):
        p = 3 * i
        x, y, z = pos[p], pos[p + 1], pos[p + 2]
        mnx = min(mnx, x); mxx = max(mxx, x)  # noqa: E702
        mny = min(mny, y); mxy = max(mxy, y)  # noqa: E702
        mnz = min(mnz, z); mxz = max(mxz, z)  # noqa: E702
    return (mnx, mny, mnz, mxx, mxy, mxz)


def _diag(b):
    dx = b[3] - b[0]
    dy = b[4] - b[1]
    dz = b[5] - b[2]
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _opt_depth(max_depth):
    if max_depth is None:
        return DEFAULT_MAX_DEPTH
    if not _is_int(max_depth) or max_depth < 0:
        _throw("max_depth must be a non-negative integer")
    return DEFAULT_MAX_DEPTH if max_depth > DEFAULT_MAX_DEPTH else max_depth


# ============================================================
# _build_tree(cloud, max_depth) -> {root, bounds, diagonal}
#   Additive quadtree over the ground plane (X/Z; Y is up); every point
#   lands in EXACTLY ONE node (no loss, no duplication).
# ============================================================
def _split(cloud, indices, level, minx, maxx, minz, maxz, depth_cap):
    node = {"level": level, "own_idx": [], "children": []}
    if level >= depth_cap or len(indices) <= LEAF_MAX:
        node["own_idx"] = indices     # leaf: keep everything that reached here
        return node
    keep = []
    rest = []
    for k, idx in enumerate(indices):
        (keep if k % LEVEL_STRIDE == 0 else rest).append(idx)
    node["own_idx"] = keep
    midx = (minx + maxx) / 2
    midz = (minz + maxz) / 2
    quads = ([], [], [], [])
    pos = cloud["positions"]
    for idx in rest:
        p = 3 * idx
        qx = 1 if pos[p] >= midx else 0
        qz = 1 if pos[p + 2] >= midz else 0
        quads[qz * 2 + qx].append(idx)
    xb = ((minx, midx), (midx, maxx))
    zb = ((minz, midz), (midz, maxz))
    for qi in range(4):
        if not quads[qi]:
            continue
        cqx = qi & 1
        cqz = (qi >> 1) & 1
        node["children"].append(_split(
            cloud, quads[qi], level + 1,
            xb[cqx][0], xb[cqx][1], zb[cqz][0], zb[cqz][1], depth_cap))
    return node


def _box_from_extent(e):
    """Axis-aligned local extent (glTF Y-up) -> 3D-Tiles boundingVolume.box in
    TILE-FRAME (a,b,c)=(x,-z,y): [center(3), xHalf(3), yHalf(3), zHalf(3)]."""
    cx = (e[0] + e[3]) / 2
    cy = (e[1] + e[4]) / 2
    cz = (e[2] + e[5]) / 2
    hx = (e[3] - e[0]) / 2
    hy = (e[4] - e[1]) / 2
    hz = (e[5] - e[2]) / 2
    return [cx, -cz, cy, hx, 0, 0, 0, hz, 0, 0, 0, hy]


def _assign(cloud, node, diag):
    """Bottom-up subtree extent -> per-node tile-frame box + geometricError
    (diagonal / 2^level — strictly decreasing down the tree)."""
    pos = cloud["positions"]
    mnx = mny = mnz = math.inf
    mxx = mxy = mxz = -math.inf
    for idx in node["own_idx"]:
        p = 3 * idx
        x, y, z = pos[p], pos[p + 1], pos[p + 2]
        mnx = min(mnx, x); mxx = max(mxx, x)  # noqa: E702
        mny = min(mny, y); mxy = max(mxy, y)  # noqa: E702
        mnz = min(mnz, z); mxz = max(mxz, z)  # noqa: E702
    for child in node["children"]:
        ce = _assign(cloud, child, diag)
        mnx = min(mnx, ce[0]); mxx = max(mxx, ce[3])  # noqa: E702
        mny = min(mny, ce[1]); mxy = max(mxy, ce[4])  # noqa: E702
        mnz = min(mnz, ce[2]); mxz = max(mxz, ce[5])  # noqa: E702
    node["geometric_error"] = diag / (2 ** node["level"])
    extent = (mnx, mny, mnz, mxx, mxy, mxz)
    node["box"] = _box_from_extent(extent)
    return extent


def _build_tree(cloud, max_depth=None):
    n = _validate_cloud(cloud)
    depth_cap = _opt_depth(max_depth)
    b = _bounds(cloud["positions"], n)
    all_idx = list(range(n))
    if n <= SINGLE_TILE_MAX or depth_cap == 0:
        root = {"level": 0, "own_idx": all_idx, "children": []}
    else:
        root = _split(cloud, all_idx, 0, b[0], b[3], b[2], b[5], depth_cap)
    diag = _diag(b)
    _assign(cloud, root, diag)
    return {"root": root,
            "bounds": {"min": [b[0], b[1], b[2]], "max": [b[3], b[4], b[5]]},
            "diagonal": diag}


# ============================================================
# _root_transform(origin, heading_offset, heading_flip) -> [16 floats]
#   Column-major 4x4 ECEF placement of the tile-frame at the origin.
# ============================================================
def _root_transform(origin, heading_offset=None, heading_flip=False):
    if not isinstance(origin, dict):
        _throw("origin {lat,lng} required")
    lat = origin.get("lat")
    lng = origin.get("lng")
    if not _fin(lat):
        _throw("origin.lat must be a finite number")
    if not _fin(lng):
        _throw("origin.lng must be a finite number")
    if abs(lat) > 90:
        _throw("origin.lat |lat| must be <= 90")
    alt = origin.get("alt")
    if alt is None:
        alt = 0.0
    elif not _fin(alt):
        _throw("origin.alt must be a finite number")
    ho = 0.0 if heading_offset is None else heading_offset
    if not _fin(ho):
        _throw("heading_offset must be a finite number")
    eff = (ho + (180 if heading_flip else 0)) * _DEG

    lat_r = lat * _DEG
    lng_r = lng * _DEG
    s_lat, c_lat = math.sin(lat_r), math.cos(lat_r)
    s_lng, c_lng = math.sin(lng_r), math.cos(lng_r)

    # WGS84 -> ECEF (closed form).
    e2 = _WGS84_F * (2 - _WGS84_F)
    big_n = _WGS84_A / math.sqrt(1 - e2 * s_lat * s_lat)
    ox = (big_n + alt) * c_lat * c_lng
    oy = (big_n + alt) * c_lat * s_lng
    oz = (big_n * (1 - e2) + alt) * s_lat

    # ENU unit basis at the origin, in ECEF.
    ex, ey, ez = -s_lng, c_lng, 0.0
    nx, ny, nz = -s_lat * c_lng, -s_lat * s_lng, c_lat
    ux, uy, uz = c_lat * c_lng, c_lat * s_lng, s_lat

    ce, se = math.cos(eff), math.sin(eff)
    # Column-major: col_a = ce*E - se*N, col_b = se*E + ce*N, col_c = Up.
    return [
        ce * ex - se * nx, ce * ey - se * ny, ce * ez - se * nz, 0,
        se * ex + ce * nx, se * ey + ce * ny, se * ez + ce * nz, 0,
        ux, uy, uz, 0,
        ox, oy, oz, 1,
    ]


# ---- GLB encoder ------------------------------------------------------------
def _f32_list(values, what):
    """Round every value through IEEE float32 (Float32Array parity). An
    overflow (JS would store Infinity and then throw) raises malformed."""
    try:
        return array("f", values)
    except (OverflowError, TypeError, ValueError):
        _throw("GLB {} must be finite float32 values".format(what))


def _encode_glb(positions, colors):
    """A valid glTF 2.0 GLB: VEC3 float32 POSITION + VEC4 uint8-normalized
    COLOR_0 (alpha 255 — a VEC3 UNSIGNED_BYTE attribute has a 3-byte element
    size, which the Khronos validator rejects as unaligned), one point-mode
    (0) primitive, KHR_materials_unlit, 4-byte chunk alignment throughout."""
    if positions is None or not hasattr(positions, "__len__"):
        _throw("GLB positions required")
    if colors is None or not hasattr(colors, "__len__"):
        _throw("GLB colors required")
    if len(positions) % 3 != 0 or len(positions) == 0:
        _throw("GLB positions length must be a positive multiple of 3")
    m = len(positions) // 3
    if len(colors) != 3 * m:
        _throw("GLB colors length must equal positions length")

    pos32 = _f32_list(positions, "positions")
    mnx = mny = mnz = math.inf
    mxx = mxy = mxz = -math.inf
    for i in range(m):
        p = 3 * i
        x, y, z = pos32[p], pos32[p + 1], pos32[p + 2]
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            _throw("GLB position[{}] not finite".format(i))
        mnx = min(mnx, x); mxx = max(mxx, x)  # noqa: E702
        mny = min(mny, y); mxy = max(mxy, y)  # noqa: E702
        mnz = min(mnz, z); mxz = max(mxz, z)  # noqa: E702
        if not (_color_ok(colors[p]) and _color_ok(colors[p + 1])
                and _color_ok(colors[p + 2])):
            _throw("GLB color[{}] must be integers 0..255".format(i))

    pos_len = 12 * m
    col_len = 4 * m
    bin_len = pos_len + col_len
    bin_pad = (4 - (bin_len % 4)) % 4

    gltf = {
        "asset": {"version": "2.0", "generator": GENERATOR},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0, "COLOR_0": 1},
            "mode": 0, "material": 0}]}],
        "materials": [{
            "pbrMetallicRoughness": {
                "baseColorFactor": [1, 1, 1, 1],
                "metallicFactor": 0, "roughnessFactor": 1},
            "extensions": {"KHR_materials_unlit": {}},
        }],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": m,
             "type": "VEC3", "min": _jlist([mnx, mny, mnz]),
             "max": _jlist([mxx, mxy, mxz])},
            {"bufferView": 1, "componentType": 5121, "normalized": True,
             "count": m, "type": "VEC4"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": pos_len,
             "target": 34962},
            {"buffer": 0, "byteOffset": pos_len, "byteLength": col_len,
             "target": 34962},
        ],
        "buffers": [{"byteLength": bin_len}],
    }
    json_bytes = _json_stringify(gltf).encode("ascii")
    json_pad = (4 - (len(json_bytes) % 4)) % 4
    json_len = len(json_bytes) + json_pad
    total = 12 + 8 + json_len + 8 + (bin_len + bin_pad)

    out = bytearray()
    out += struct.pack("<III", 0x46546C67, 2, total)         # 'glTF', v2, len
    out += struct.pack("<II", json_len, 0x4E4F534A)           # JSON chunk
    out += json_bytes
    out += b" " * json_pad
    out += struct.pack("<II", bin_len + bin_pad, 0x004E4942)  # BIN chunk
    out += struct.pack("<{}f".format(3 * m), *pos32)
    rgba = bytearray(4 * m)
    for i in range(m):
        p = 3 * i
        o = 4 * i
        rgba[o] = int(colors[p])
        rgba[o + 1] = int(colors[p + 1])
        rgba[o + 2] = int(colors[p + 2])
        rgba[o + 3] = 255
    out += rgba
    out += b"\x00" * bin_pad
    return bytes(out)


# ---- plan ------------------------------------------------------------------
def _plan(cloud, opts):
    opts = opts or {}
    tree = _build_tree(cloud, opts.get("max_depth"))
    pos = cloud["positions"]
    col = cloud["colors"]
    content_nodes = []

    def tile_json(node):
        uri = "{}.glb".format(len(content_nodes))    # pre-order index -> uri
        own = node["own_idx"]
        m = len(own)
        np = array("f", bytes(12 * m))
        nc = bytearray(3 * m)
        for i, idx in enumerate(own):
            p = 3 * idx
            o = 3 * i
            np[o] = pos[p]
            np[o + 1] = pos[p + 1]
            np[o + 2] = pos[p + 2]
            nc[o] = col[p]
            nc[o + 1] = col[p + 1]
            nc[o + 2] = col[p + 2]
        content_nodes.append({"uri": uri, "pos": np, "col": nc})
        has_children = len(node["children"]) > 0
        tile = {
            "boundingVolume": {"box": _jlist(node["box"])},
            "geometricError": _jnum(node["geometric_error"])
            if has_children else 0,
            "content": {"uri": uri},
        }
        if has_children:
            tile["children"] = [tile_json(c) for c in node["children"]]
        return tile

    root_tile = tile_json(tree["root"])
    root_tile["refine"] = "ADD"
    if opts.get("origin") is not None:
        root_tile["transform"] = _jlist(_root_transform(
            opts["origin"], opts.get("heading_offset"),
            bool(opts.get("heading_flip"))))
    tileset = {
        "asset": {"version": "1.1"},
        "geometricError": _jnum(tree["diagonal"]) if tree["diagonal"] else 0,
        "root": root_tile,
    }
    return {"tileset": tileset, "content_nodes": content_nodes,
            "bounds": tree["bounds"], "tile_count": len(content_nodes)}


# ============================================================
# build_tileset(cloud, opts?) -> {files, tile_count, bounds}
# ============================================================
def build_tileset(cloud, opts=None):
    """Build the full tileset for a validated cloud.

    ``cloud``: ``{positions (x,y,z * n, local metric Y-up), colors
    (r,g,b * n ints 0..255), point_count}`` — the exact
    ``pointcloud.to_cloud()['cloud']`` shape. ``opts`` (all optional):
    ``origin`` ``{lat,lng,alt}`` (georeferenced root transform),
    ``heading_offset`` (degrees), ``heading_flip`` (bool), ``max_depth``
    (0..3).

    Returns ``{'files': [{'path': str, 'bytes': bytes}, ...] (files[0] is the
    root tileset.json), 'tile_count': int, 'bounds': [minx,miny,minz,
    maxx,maxy,maxz] (LOCAL metres) | None}``. Deterministic — identical input
    always yields identical bytes."""
    plan = _plan(cloud, opts)
    files = [{
        "path": "tileset.json",
        "bytes": _json_stringify(plan["tileset"]).encode("ascii"),
    }]
    for node in plan["content_nodes"]:
        files.append({"path": node["uri"],
                      "bytes": _encode_glb(node["pos"], node["col"])})
    b = plan["bounds"]
    bounds = (list(b["min"]) + list(b["max"])) if b else None
    return {"files": files, "tile_count": plan["tile_count"],
            "bounds": bounds}
