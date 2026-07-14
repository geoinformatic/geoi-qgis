"""Point-cloud readers + CRS handling for the 3D Tiles publish path.

A faithful pure-Python port of the geoi web app's point-cloud import lane
(``src/scan3d/laz-export.js`` decode / ``ply-export.js`` decode /
``las-crs.js`` / ``pointcloud-import.js`` toCloud / the ``georef.js`` +
``point-cloud.js`` helpers it leans on), so the QGIS plugin can turn a
``.las`` / ``.laz`` / ``.ply`` upload into the exact LOCAL metric Y-up cloud
shape ``tiles3d_encoder.build_tileset`` consumes.

Pure / stdlib-only — NO QGIS or PyQt imports (unit-testable off a QGIS
install; the GUI wiring lives in ``plugin.py`` / ``tasks.py``). Deterministic:
no wall-clock, no randomness.

HOSTILE INPUT: an uploaded file is untrusted. Every count/offset/length read
from the bytes is bounded against the ACTUAL buffer BEFORE any allocation
(mirroring the JS decoders' posture), so a forged header can never drive a
huge allocation — it raises a ``malformed``-flagged :class:`PointCloudError`.

``.laz`` handling is a ladder (the browser uses a laz-perf WASM; pure Python
has no LASzip): try the ``laspy`` package (with a working LAZ backend), else
the ``pdal`` executable, else raise a friendly "decompress it first" error.
"""

import io
import math
import os
import re
import shutil
import struct
# security review: see _laz_via_pdal below — fixed executable, list-form argv
import subprocess  # nosec B404
import tempfile
from array import array

# Same size/capability posture as the web path (pointcloud-import.js) — kept
# in lock-step so a QGIS import and a web import publish identically.
PUBLISH_TARGET_POINTS = 1500000   # over this => transparently downsample
PUBLISH_MAX_POINTS = 8000000      # over this => refuse (too detailed)
DEFAULT_GREY = 160                # flat mid-grey for an uncoloured cloud

# ---- WGS84 / UTM constants (las-crs.js) -----------------------------------
_A = 6378137.0                    # WGS84 semi-major axis, metres
_F = 1.0 / 298.257223563          # WGS84 flattening
_K0 = 0.9996                      # UTM scale factor
_FE = 500000.0                    # UTM false easting
_E2 = _F * (2 - _F)               # first eccentricity squared
_E4 = _E2 * _E2
_E6 = _E4 * _E2
_EP2 = _E2 / (1 - _E2)            # second eccentricity squared
_R = 6378137.0                    # georef.js sphere radius (ENU conversions)
_DEG = math.pi / 180.0
_RAD = 180.0 / math.pi

# ---- LAS layout constants (laz-export.js) ---------------------------------
_LAS_HEADER_MIN = 227             # legacy (1.1-1.3) Public Header Block min
_LAS_HEADER_14 = 375              # LAS 1.4 Public Header Block
_VLR_HEADER = 54                  # VLR header (no payload)
# Per ASPRS LAS 1.1-1.4: X/Y/Z (i32 @ 0/4/8) + intensity (u16 @ 12) are
# identical in EVERY point format; only GPS time and RGB move. Each entry is
# (min record bytes, gps offset or -1, rgb offset or -1).
_PF_FORMATS = {
    0: (20, -1, -1), 1: (28, 20, -1), 2: (26, -1, 20), 3: (34, 20, 28),
    4: (57, 20, -1), 5: (63, 20, 28), 6: (30, 22, -1), 7: (36, 22, 30),
    8: (38, 22, 30), 9: (59, 22, -1), 10: (67, 22, 30),
}

# ---- PLY scalar property widths (ply-export.js) ---------------------------
_PLY_TYPE_SIZE = {
    "char": 1, "int8": 1, "uchar": 1, "uint8": 1,
    "short": 2, "int16": 2, "ushort": 2, "uint16": 2,
    "int": 4, "int32": 4, "uint": 4, "uint32": 4,
    "float": 4, "float32": 4, "double": 8, "float64": 8,
}
_PLY_FLOAT_TYPE = ("float", "float32", "double", "float64")
_PLY_STRUCT_FMT = {
    "char": "b", "int8": "b", "uchar": "B", "uint8": "B",
    "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
    "int": "i", "int32": "i", "uint": "I", "uint32": "I",
    "float": "f", "float32": "f", "double": "d", "float64": "d",
}
_PLY_MAX_HEADER = 65536           # a PLY header far beyond this is malformed


class PointCloudError(Exception):
    """An actionable, user-facing point-cloud error. ``str(exc)`` is safe to
    show verbatim in a dialog. Flags mirror the JS error posture:
    ``malformed`` (corrupt/forged bytes), ``unsupported`` (valid but not a
    usable format/CRS), ``too_large`` (valid but over the point cap)."""

    def __init__(self, message, malformed=False, unsupported=False,
                 too_large=False, reason=None):
        super().__init__(message)
        self.malformed = malformed
        self.unsupported = unsupported
        self.too_large = too_large
        self.reason = reason


def _malformed(msg):
    raise PointCloudError(msg, malformed=True)


def _fin(v):
    return isinstance(v, (int, float)) and math.isfinite(v)


def _js_round(v):
    """JS ``Math.round`` — half-up (Python's ``round`` is banker's)."""
    return math.floor(v + 0.5)


# =============================================================== LAS reader
def _trim_ascii(data, off, length):
    """ASCII field of ``length`` bytes, stopped at the first NUL."""
    raw = data[off:off + length]
    nul = raw.find(b"\x00")
    if nul >= 0:
        raw = raw[:nul]
    return raw.decode("latin-1")


def _epsg_from_wkt(wkt):
    """The CRS's own EPSG code is the LAST ``AUTHORITY["EPSG","<code>"]`` in
    a WKT string — inner DATUM/SPHEROID authorities precede the outermost
    node's, which closes last. Returns a positive int or None."""
    last = None
    for m in re.finditer(r'AUTHORITY\s*\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]', wkt,
                         re.IGNORECASE):
        last = m.group(1)
    if last is None:
        return None
    n = int(last)
    return n if n > 0 else None


def _geokey_epsg(data, payload, rec_len):
    """Inline EPSG from a GeoKeyDirectoryTag (34735) payload: a 4-u16 header
    then NumberOfKeys x 4 u16 keys. ProjectedCSTypeGeoKey (3072) wins over
    GeographicTypeGeoKey (2048); only inline (TIFFTagLocation 0) values are
    usable. The key count is bounded by the declared payload length."""
    if rec_len < 8:
        return None
    (num_keys,) = struct.unpack_from("<H", data, payload + 6)
    max_keys = (rec_len - 8) // 8
    if num_keys > max_keys:
        num_keys = max_keys
    found = None
    for k in range(num_keys):
        base = payload + 8 + k * 8
        key_id, tiff_loc = struct.unpack_from("<HH", data, base)
        (value,) = struct.unpack_from("<H", data, base + 6)
        if tiff_loc == 0 and key_id == 3072 and value > 0:
            return value
        if tiff_loc == 0 and key_id == 2048 and value > 0 and found is None:
            found = value
    return found


def _parse_crs_vlrs(data, offset_to_point):
    """Walk the VLRs between the Public Header Block and the point data for a
    CRS hint (OGC WKT record 2112 / GeoKeyDirectoryTag 34735). Additive:
    returns None when no recognised projection VLR is present. HOSTILE INPUT
    — the VLR count and every declared record length are bounded against the
    VLR region BEFORE any payload read; an overrun raises ``malformed``."""
    (vlr_count,) = struct.unpack_from("<I", data, 100)
    (header_end,) = struct.unpack_from("<H", data, 94)
    # A bogus header size / count just means "no VLRs to read" — not a hard
    # error (the point block already validated).
    if header_end < _LAS_HEADER_MIN or header_end > offset_to_point:
        return None
    region = offset_to_point - header_end
    if vlr_count > region // _VLR_HEADER:
        _malformed("VLR count {} overruns the VLR region".format(vlr_count))
    pos = header_end
    epsg = None
    wkt = None
    for _ in range(vlr_count):
        if pos + _VLR_HEADER > offset_to_point:
            _malformed("VLR header overruns the buffer")
        (record_id, rec_len) = struct.unpack_from("<HH", data, pos + 18)
        payload = pos + _VLR_HEADER
        if payload + rec_len > offset_to_point:
            _malformed("VLR payload overruns the buffer")
        user_id = _trim_ascii(data, pos + 2, 16)
        if record_id == 2112 and user_id == "LASF_Projection":
            s = _trim_ascii(data, payload, rec_len)
            if s:
                wkt = s
                if epsg is None:
                    epsg = _epsg_from_wkt(s)
        elif record_id == 34735 and user_id == "LASF_Projection":
            e = _geokey_epsg(data, payload, rec_len)
            if e and epsg is None:
                epsg = e
        pos = payload + rec_len
    if epsg is None and wkt is None:
        return None
    return {"epsg": epsg, "wkt": wkt}


def _is_laz(data):
    """LASzip flags compression by setting the TOP bit of the Point Data
    Record Format byte (offset 104)."""
    return len(data) > 104 and (data[104] & 0x80) != 0


def read_las(data):
    """Decode ANY ASPRS LAS 1.1-1.4 file, point format 0-10 (a LASzip ``.laz``
    is decompressed first via the laspy -> pdal ladder). Mirrors the JS
    ``Scan3DLasExport.decode`` shape::

        {point_count, positions (x,y,z * n doubles; raw*scale+offset),
         colors (r,g,b * n 0..255 or None), intensities (n u16),
         gps_time (n doubles or None),
         bounds {min_lng,max_lng,min_lat,max_lat,min_alt,max_alt} (header),
         crs {epsg, wkt} or None}
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _malformed("bytes required")
    data = bytes(data)
    if len(data) < _LAS_HEADER_MIN:
        _malformed("too short for a LAS header")
    if data[0:4] != b"LASF":
        _malformed("bad LAS signature")
    if _is_laz(data):
        data = _decompress_laz(data)
        if len(data) < _LAS_HEADER_MIN or data[0:4] != b"LASF":
            _malformed("the decompressed .laz is not a valid LAS file")

    version_minor = data[25]
    fmt_id = data[104]
    fmt = _PF_FORMATS.get(fmt_id)
    if fmt is None:
        _malformed("unsupported point data record format {}".format(fmt_id))
    fmt_len, gps_off, rgb_off = fmt

    (offset_to_point,) = struct.unpack_from("<I", data, 96)
    (pdr_len,) = struct.unpack_from("<H", data, 105)

    # Point count: the legacy u32 (offset 107) — UNLESS LAS 1.4 with a zero
    # legacy field, where the authoritative 64-bit count lives at offset 247.
    (point_count,) = struct.unpack_from("<I", data, 107)
    if version_minor >= 4:
        if len(data) < _LAS_HEADER_14:
            _malformed("too short for a LAS 1.4 header")
        if point_count == 0:
            (point_count,) = struct.unpack_from("<Q", data, 247)

    x_scale, y_scale, z_scale, x_off, y_off, z_off = struct.unpack_from(
        "<6d", data, 131)
    (max_lng, min_lng, max_lat, min_lat, max_alt, min_alt) = struct.unpack_from(
        "<6d", data, 179)
    bounds = {
        "max_lng": max_lng, "min_lng": min_lng,
        "max_lat": max_lat, "min_lat": min_lat,
        "max_alt": max_alt, "min_alt": min_alt,
    }

    # ---- hostile-input bounds (BEFORE any allocation) ----------------------
    if pdr_len < fmt_len:
        _malformed("point record length {} too small for format {}".format(
            pdr_len, fmt_id))
    if point_count < 0:
        _malformed("invalid point count")
    if offset_to_point < _LAS_HEADER_MIN or offset_to_point > len(data):
        _malformed("bad point-data offset")
    if offset_to_point + pdr_len * point_count > len(data):
        _malformed("truncated point block")

    crs = _parse_crs_vlrs(data, offset_to_point)

    has_gps = gps_off >= 0
    has_rgb = rgb_off >= 0
    positions = array("d", bytes(24 * point_count))
    intensities = array("H", bytes(2 * point_count))
    colors = bytearray(3 * point_count) if has_rgb else None
    gps_time = array("d", bytes(8 * point_count)) if has_gps else None

    s_xyz = struct.Struct("<iii")
    s_u16 = struct.Struct("<H")
    s_f64 = struct.Struct("<d")
    s_rgb = struct.Struct("<HHH")
    for i in range(point_count):
        rec = offset_to_point + pdr_len * i
        p = 3 * i
        xr, yr, zr = s_xyz.unpack_from(data, rec)
        positions[p] = xr * x_scale + x_off        # lng (or projected X)
        positions[p + 1] = yr * y_scale + y_off    # lat (or projected Y)
        positions[p + 2] = zr * z_scale + z_off    # alt
        intensities[i] = s_u16.unpack_from(data, rec + 12)[0]
        if has_gps:
            gps_time[i] = s_f64.unpack_from(data, rec + gps_off)[0]
        if has_rgb:
            r, g, b = s_rgb.unpack_from(data, rec + rgb_off)
            colors[p] = _js_round(r / 257)         # 16-bit -> 8-bit
            colors[p + 1] = _js_round(g / 257)
            colors[p + 2] = _js_round(b / 257)

    return {
        "point_count": point_count,
        "positions": positions,
        "colors": colors,
        "intensities": intensities,
        "gps_time": gps_time,
        "bounds": bounds,
        "crs": crs,
    }


# ---------------------------------------------------------- .laz ladder
_LAZ_HELP = (
    "This .laz file is LASzip-compressed and no decompressor is available "
    "here. Decompress the .laz to .las first (or install the 'laspy' Python "
    "package with a LAZ backend, e.g. 'pip install laspy[lazrs]', or the "
    "'pdal' command-line tool), then try again."
)


def _laz_via_laspy(data):
    """Decompress via the ``laspy`` package (needs a lazrs/laszip backend).
    Returns uncompressed ``.las`` bytes, or None when laspy (or its LAZ
    backend) is unavailable / fails — the ladder then tries pdal."""
    try:
        import laspy  # noqa: F401 - optional dependency, probed at runtime
    except Exception:  # noqa: BLE001 - absent/broken install => next rung
        return None
    try:
        las = laspy.open(io.BytesIO(data)).read()
        out = io.BytesIO()
        las.write(out, do_compress=False)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - no LAZ backend / corrupt => next rung
        return None


def _laz_via_pdal(data):
    """Decompress via the ``pdal`` executable (``pdal translate`` to a temp
    ``.las``). Returns the ``.las`` bytes, or None when pdal is absent or the
    translate fails."""
    exe = shutil.which("pdal")
    if not exe:
        return None
    tmp = tempfile.mkdtemp(prefix="geoi-laz-")
    try:
        src = os.path.join(tmp, "in.laz")
        dst = os.path.join(tmp, "out.las")
        with open(src, "wb") as fh:
            fh.write(data)
        # security review: `exe` is resolved via shutil.which("pdal") — a
        # fixed, known executable name — never user input; list-form argv,
        # no shell=True, and src/dst are paths this function created itself
        # inside its own mkdtemp() sandbox, so there is no injection surface.
        subprocess.run([exe, "translate", src, dst], check=True,  # nosec B603
                       capture_output=True, timeout=600)
        with open(dst, "rb") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001 - any pdal failure => ladder exhausted
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _decompress_laz(data):
    """The ``.laz`` ladder: laspy -> pdal -> a friendly, actionable error."""
    las = _laz_via_laspy(data)
    if las is None:
        las = _laz_via_pdal(data)
    if las is None:
        raise PointCloudError(_LAZ_HELP, unsupported=True)
    return las


# =============================================================== PLY reader
def _ply_color_byte(v, type_name):
    """A colour channel read as its declared type -> an 8-bit 0..255 byte:
    uchar verbatim, ushort scaled down (/257), float treated as 0..1. A
    non-finite value degrades like the JS Uint8Array store (NaN -> 0,
    +/-Inf clamped) instead of raising."""
    if not math.isfinite(v):
        return 255 if v == math.inf else 0
    if type_name in _PLY_FLOAT_TYPE:
        out = _js_round(v * 255)
    elif _PLY_TYPE_SIZE[type_name] >= 2:
        out = _js_round(v / 257)
    else:
        out = _js_round(v)
    return 0 if out < 0 else (255 if out > 255 else out)


def read_ply(data):
    """Decode ANY Stanford PLY — ascii / binary_little_endian /
    binary_big_endian — reading x/y/z (required) + red/green/blue | r/g/b
    (optional -> ``colors`` None) and accounting for every other declared
    property's width so the stride stays exact. Mirrors the JS
    ``Scan3DPlyExport.decode`` shape::

        {point_count, positions (x,y,z * n), colors (or None),
         bounds {'min': [x,y,z], 'max': [x,y,z]}}
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _malformed("bytes required")
    data = bytes(data)
    if len(data) < 4 or data[0:3] != b"ply":
        _malformed('not a PLY file (missing "ply" magic)')

    # ---- header: ASCII lines up to end_header ------------------------------
    lines = []
    body_offset = -1
    line_start = 0
    scan_limit = min(len(data), _PLY_MAX_HEADER)
    for pos in range(scan_limit):
        if data[pos] != 0x0A:
            continue
        line = data[line_start:pos].replace(b"\r", b"").decode("latin-1")
        lines.append(line)
        line_start = pos + 1
        if line.strip() == "end_header":
            body_offset = pos + 1
            break
    if body_offset < 0:
        _malformed("missing end_header (or header too long)")
    if len(lines) < 2 or lines[0].strip() != "ply":
        _malformed("bad PLY header")

    # ---- format + element/property declarations ----------------------------
    fmt_name = None
    elements = []
    cur = None
    for line in lines[1:]:
        tok = line.strip().split()
        if not tok:
            continue
        kw = tok[0]
        if kw == "format":
            fmt_name = tok[1] if len(tok) > 1 else None
            if len(tok) < 3 or tok[2] != "1.0":
                _malformed("unsupported PLY version {}".format(
                    tok[2] if len(tok) > 2 else "?"))
        elif kw == "element":
            if len(tok) < 3 or not tok[2].isdigit() or str(int(tok[2])) != tok[2]:
                _malformed('bad element count "{}"'.format(
                    tok[2] if len(tok) > 2 else "?"))
            cur = {"name": tok[1], "count": int(tok[2]), "props": []}
            elements.append(cur)
        elif kw == "property":
            if cur is None:
                _malformed("property before element")
            if len(tok) > 1 and tok[1] == "list":
                cur["props"].append({"list": True,
                                     "name": tok[4] if len(tok) > 4 else ""})
                continue
            type_name = tok[1] if len(tok) > 1 else ""
            if type_name not in _PLY_TYPE_SIZE:
                _malformed("unknown property type {}".format(type_name))
            cur["props"].append({"type": type_name,
                                 "name": tok[2] if len(tok) > 2 else "",
                                 "size": _PLY_TYPE_SIZE[type_name]})
        elif kw == "end_header":
            break
        # comment / obj_info / anything else -> ignored
    if fmt_name not in ("ascii", "binary_little_endian", "binary_big_endian"):
        _malformed('unsupported PLY format "{}"'.format(fmt_name))

    # The vertex element MUST be the first data block, else its byte/token
    # offset would depend on preceding elements' sizes (unsupported layout).
    if not elements or elements[0]["name"] != "vertex":
        _malformed("PLY must start with a vertex element")
    props = elements[0]["props"]
    n = elements[0]["count"]

    idx = {"x": -1, "y": -1, "z": -1, "r": -1, "g": -1, "b": -1}
    stride = 0
    prop_off = []
    for pi, pr in enumerate(props):
        if pr.get("list"):
            _malformed("list property in vertex element is unsupported")
        prop_off.append(stride)
        stride += pr["size"]
        nm = pr["name"]
        if nm == "x":
            idx["x"] = pi
        elif nm == "y":
            idx["y"] = pi
        elif nm == "z":
            idx["z"] = pi
        elif nm in ("red", "r"):
            idx["r"] = pi
        elif nm in ("green", "g"):
            idx["g"] = pi
        elif nm in ("blue", "b"):
            idx["b"] = pi
    if idx["x"] < 0 or idx["y"] < 0 or idx["z"] < 0:
        _malformed("vertex is missing x/y/z")
    has_color = idx["r"] >= 0 and idx["g"] >= 0 and idx["b"] >= 0

    # Positions land in float32 (Float32Array parity with the JS decoder).
    positions = array("f", bytes(12 * n))
    colors = bytearray(3 * n) if has_color else None
    mnx = mny = mnz = math.inf
    mxx = mxy = mxz = -math.inf

    def _f(tok_text, i):
        try:
            v = float(tok_text)
        except (TypeError, ValueError):
            v = math.nan
        if not math.isfinite(v):
            _malformed("non-finite ascii vertex {}".format(i))
        return v

    if fmt_name == "ascii":
        toks = data[body_offset:].decode("latin-1").split()
        per = len(props)
        # Bound the declared vertex count against the tokens actually present
        # (trailing elements, e.g. faces, may follow — hence >=, not ==).
        if n > 0 and len(toks) < per * n:
            _malformed("ascii body shorter than declared vertex count")
        for ai in range(n):
            base = ai * per
            o = 3 * ai
            vx = _f(toks[base + idx["x"]], ai)
            vy = _f(toks[base + idx["y"]], ai)
            vz = _f(toks[base + idx["z"]], ai)
            positions[o] = vx
            positions[o + 1] = vy
            positions[o + 2] = vz
            mnx = min(mnx, vx); mxx = max(mxx, vx)  # noqa: E702
            mny = min(mny, vy); mxy = max(mxy, vy)  # noqa: E702
            mnz = min(mnz, vz); mxz = max(mxz, vz)  # noqa: E702
            if has_color:
                for ch, key in ((0, "r"), (1, "g"), (2, "b")):
                    pr = props[idx[key]]
                    try:
                        raw = float(toks[base + idx[key]])
                    except (TypeError, ValueError):
                        raw = 0.0
                    colors[o + ch] = _ply_color_byte(raw, pr["type"])
    else:
        le = "<" if fmt_name == "binary_little_endian" else ">"
        # Hostile-input bound: the vertex block must fit the buffer BEFORE we
        # index into it (this also caps n, since stride >= 3 bytes > 0).
        if stride <= 0:
            _malformed("zero-width vertex record")
        if body_offset + stride * n > len(data):
            _malformed("binary body shorter than declared vertex count")
        readers = [struct.Struct(le + _PLY_STRUCT_FMT[pr["type"]])
                   for pr in props]
        for bi in range(n):
            rec = body_offset + stride * bi
            o = 3 * bi
            bx = readers[idx["x"]].unpack_from(data, rec + prop_off[idx["x"]])[0]
            by = readers[idx["y"]].unpack_from(data, rec + prop_off[idx["y"]])[0]
            bz = readers[idx["z"]].unpack_from(data, rec + prop_off[idx["z"]])[0]
            if not (math.isfinite(bx) and math.isfinite(by) and math.isfinite(bz)):
                _malformed("non-finite binary vertex {}".format(bi))
            positions[o] = bx
            positions[o + 1] = by
            positions[o + 2] = bz
            mnx = min(mnx, bx); mxx = max(mxx, bx)  # noqa: E702
            mny = min(mny, by); mxy = max(mxy, by)  # noqa: E702
            mnz = min(mnz, bz); mxz = max(mxz, bz)  # noqa: E702
            if has_color:
                for ch, key in ((0, "r"), (1, "g"), (2, "b")):
                    pr = props[idx[key]]
                    raw = readers[idx[key]].unpack_from(
                        data, rec + prop_off[idx[key]])[0]
                    colors[o + ch] = _ply_color_byte(raw, pr["type"])

    bounds = ({"min": [mnx, mny, mnz], "max": [mxx, mxy, mxz]} if n
              else {"min": [0, 0, 0], "max": [0, 0, 0]})
    return {"point_count": n, "positions": positions, "colors": colors,
            "bounds": bounds}


# ============================================================ CRS detection
def detect_crs(crs_hint, sample_bounds):
    """Port of ``Scan3DLasCrs.detect``. ``crs_hint`` is ``{epsg, wkt}`` (or
    None); ``sample_bounds`` carries the LAS header raw X/Y as
    ``{min_lng,max_lng,min_lat,max_lat}`` (named lng/lat by the decode shape
    even for a projected file — only the no-hint heuristic reads it).

    Returns ``{kind: 'wgs84'|'utm'|'unsupported', zone?, hemisphere?, epsg?,
    reason?}``. Precedence: EPSG (WGS84 -> UTM N -> UTM S -> ETRS89/UTM) ->
    WKT "UTM zone NN[NS]" -> recognised-but-unsupported EPSG -> no-hint
    geographic heuristic on the raw bounds."""
    if crs_hint is not None and not isinstance(crs_hint, dict):
        _malformed("detect_crs: crs_hint must be a dict or None")
    epsg = None
    if crs_hint and _fin(crs_hint.get("epsg")):
        epsg = math.floor(crs_hint["epsg"])

    if epsg in (4326, 4979):
        return {"kind": "wgs84"}
    if epsg is not None:
        if 32601 <= epsg <= 32660:
            return {"kind": "utm", "zone": epsg - 32600, "hemisphere": "N",
                    "epsg": epsg}
        if 32701 <= epsg <= 32760:
            return {"kind": "utm", "zone": epsg - 32700, "hemisphere": "S",
                    "epsg": epsg}
        # ETRS89 / UTM (25828-25838, zones 28N-38N) — ~WGS84 at sub-metre
        # accuracy, common for European LAS; treat as UTM North.
        if 25828 <= epsg <= 25838:
            zone = min(max(epsg - 25800, 1), 60)
            return {"kind": "utm", "zone": zone, "hemisphere": "N",
                    "epsg": epsg}

    wkt = crs_hint.get("wkt") if crs_hint else None
    if isinstance(wkt, str) and wkt:
        m = re.search(r"UTM zone (\d{1,2})\s*([NS])", wkt, re.IGNORECASE)
        if m:
            zone = int(m.group(1))
            if 1 <= zone <= 60:
                return {"kind": "utm", "zone": zone,
                        "hemisphere": m.group(2).upper()}

    if epsg is not None:
        return {"kind": "unsupported",
                "reason": "epsg {} is not a supported projection "
                          "(WGS84 or UTM only)".format(epsg)}

    if not isinstance(sample_bounds, dict):
        _malformed("detect_crs: sample_bounds required for the heuristic")
    try:
        min_lng = sample_bounds["min_lng"]
        max_lng = sample_bounds["max_lng"]
        min_lat = sample_bounds["min_lat"]
        max_lat = sample_bounds["max_lat"]
    except KeyError:
        min_lng = max_lng = min_lat = max_lat = None
    if not (_fin(min_lng) and _fin(max_lng) and _fin(min_lat) and _fin(max_lat)):
        _malformed("detect_crs: sample_bounds needs finite min/max lng/lat")
    if -180 <= min_lng and max_lng <= 180 and -90 <= min_lat and max_lat <= 90:
        return {"kind": "wgs84"}
    return {"kind": "unsupported",
            "reason": "no CRS metadata and coordinates are out of "
                      "geographic range"}


def utm_to_wgs84(easting, northing, zone, hemisphere):
    """Closed-form Transverse-Mercator INVERSE on the WGS84 ellipsoid (the
    standard Snyder footpoint-latitude series) — a VERBATIM port of
    ``Scan3DLasCrs.utmToWgs84``. Returns ``(lat, lng)`` in degrees."""
    if not (_fin(easting) and _fin(northing)):
        _malformed("utm_to_wgs84: easting/northing must be finite")
    if not (isinstance(zone, int) and not isinstance(zone, bool)
            and 1 <= zone <= 60):
        _malformed("utm_to_wgs84: zone must be an integer in [1,60]")
    if hemisphere not in ("N", "S"):
        _malformed('utm_to_wgs84: hemisphere must be "N" or "S"')

    fn = 10000000.0 if hemisphere == "S" else 0.0
    x = easting - _FE
    y = northing - fn

    # Meridional arc -> footpoint latitude phi1 (Snyder eq. 3-24/3-26).
    big_m = y / _K0                            # origin at the equator (M0 = 0)
    mu = big_m / (_A * (1 - _E2 / 4 - 3 * _E4 / 64 - 5 * _E6 / 256))
    sq = math.sqrt(1 - _E2)
    e1 = (1 - sq) / (1 + sq)
    e1_2 = e1 * e1
    e1_3 = e1_2 * e1
    e1_4 = e1_3 * e1
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1_3 / 32) * math.sin(2 * mu)
            + (21 * e1_2 / 16 - 55 * e1_4 / 32) * math.sin(4 * mu)
            + (151 * e1_3 / 96) * math.sin(6 * mu)
            + (1097 * e1_4 / 512) * math.sin(8 * mu))

    sin_phi1 = math.sin(phi1)
    cos_phi1 = math.cos(phi1)
    tan_phi1 = math.tan(phi1)
    sin2 = sin_phi1 * sin_phi1
    c1 = _EP2 * cos_phi1 * cos_phi1
    t1 = tan_phi1 * tan_phi1
    n1 = _A / math.sqrt(1 - _E2 * sin2)
    r1 = _A * (1 - _E2) / math.pow(1 - _E2 * sin2, 1.5)
    d = x / (n1 * _K0)
    d2 = d * d
    d3 = d2 * d
    d4 = d3 * d
    d5 = d4 * d
    d6 = d5 * d

    lat = phi1 - (n1 * tan_phi1 / r1) * (
        d2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2
           - 3 * c1 * c1) * d6 / 720
    )
    lng0 = ((zone - 1) * 6 - 180 + 3) * _DEG   # central meridian, radians
    lng = lng0 + (
        d
        - (1 + 2 * t1 + c1) * d3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2
           + 24 * t1 * t1) * d5 / 120
    ) / cos_phi1

    return (lat * _RAD, lng * _RAD)


# ================================================ georef math (georef.js)
def _latlng_to_enu(o_lat, o_lng, o_alt, lat, lng, alt):
    """``Scan3DGeoref.latLngToEnu`` specialised to headingOffset 0 / no flip
    (the only configuration the import path uses): haversine distance +
    initial bearing on the R=6378137 sphere -> East/North metres; Up is the
    altitude delta. Returns ``(e, n, u)``."""
    f1 = o_lat * _DEG
    f2 = lat * _DEG
    d_f = (lat - o_lat) * _DEG
    d_l = (lng - o_lng) * _DEG
    sin_df = math.sin(d_f / 2)
    sin_dl = math.sin(d_l / 2)
    a = sin_df * sin_df + math.cos(f1) * math.cos(f2) * sin_dl * sin_dl
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist = _R * c
    y = math.sin(d_l) * math.cos(f2)
    x = math.cos(f1) * math.sin(f2) - math.sin(f1) * math.cos(f2) * math.cos(d_l)
    bearing = (math.atan2(y, x) * _RAD + 360) % 360
    az_r = bearing * _DEG
    return (dist * math.sin(az_r), dist * math.cos(az_r), alt - o_alt)


# ==================================== downsampling (point-cloud.js port)
def _min_max(pos, n):
    """``{'min': [3], 'max': [3]}`` bounds over a positions array."""
    mnx = mny = mnz = math.inf
    mxx = mxy = mxz = -math.inf
    for i in range(n):
        p = 3 * i
        x, y, z = pos[p], pos[p + 1], pos[p + 2]
        mnx = min(mnx, x); mxx = max(mxx, x)  # noqa: E702
        mny = min(mny, y); mxy = max(mxy, y)  # noqa: E702
        mnz = min(mnz, z); mxz = max(mxz, z)  # noqa: E702
    return {"min": [mnx, mny, mnz], "max": [mxx, mxy, mxz]}


def _voxel_downsample(cloud, voxel_size):
    """Grid-average: each occupied voxel collapses to the MEAN position +
    JS-rounded MEAN colour of its members. Output order = first-insertion
    order (deterministic, mirroring the JS Map walk)."""
    n = cloud["point_count"]
    pos = cloud["positions"]
    col = cloud["colors"]
    ox, oy, oz = cloud["bounds"]["min"]
    cells = {}
    for i in range(n):
        p = 3 * i
        key = (math.floor((pos[p] - ox) / voxel_size),
               math.floor((pos[p + 1] - oy) / voxel_size),
               math.floor((pos[p + 2] - oz) / voxel_size))
        cell = cells.get(key)
        if cell is None:
            cell = [0.0, 0.0, 0.0, 0, 0, 0, 0]
            cells[key] = cell
        cell[0] += pos[p]
        cell[1] += pos[p + 1]
        cell[2] += pos[p + 2]
        cell[3] += col[p]
        cell[4] += col[p + 1]
        cell[5] += col[p + 2]
        cell[6] += 1
    m = len(cells)
    positions = array("d", bytes(24 * m))
    colors = bytearray(3 * m)
    for j, cell in enumerate(cells.values()):
        inv = 1.0 / cell[6]
        o = 3 * j
        positions[o] = cell[0] * inv
        positions[o + 1] = cell[1] * inv
        positions[o + 2] = cell[2] * inv
        colors[o] = _js_round(cell[3] * inv)
        colors[o + 1] = _js_round(cell[4] * inv)
        colors[o + 2] = _js_round(cell[5] * inv)
    return {"positions": positions, "colors": colors, "point_count": m,
            "bounds": _min_max(positions, m)}


_cbrt = getattr(math, "cbrt", lambda v: v ** (1.0 / 3.0))


def _cap_points(cloud, max_points, voxel_size=None):
    """``Scan3DPointCloud.capPoints`` port: at/under the cap -> unchanged;
    else voxel-downsample with a coarsening size (x cbrt(N/max), <=5 rounds);
    if still over, deterministically TRUNCATE to the first ``max_points``."""
    if cloud["point_count"] <= max_points:
        return cloud
    vs = voxel_size
    if vs is None:
        b = cloud["bounds"]
        dx = b["max"][0] - b["min"][0]
        dy = b["max"][1] - b["min"][1]
        dz = b["max"][2] - b["min"][2]
        diag = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        vs = diag / 64  # ~64 cells across the longest extent to start
    cur = cloud
    for _ in range(5):
        if cur["point_count"] <= max_points:
            break
        vs *= _cbrt(cur["point_count"] / max_points)
        cur = _voxel_downsample(cur, vs)
    if cur["point_count"] <= max_points:
        return cur
    out = {
        "positions": cur["positions"][:3 * max_points],
        "colors": cur["colors"][:3 * max_points],
        "point_count": max_points,
        "bounds": _min_max(cur["positions"], max_points),
        "truncated": True,
    }
    return out


def _colormap_turbo(t):
    """Google Turbo colormap polynomial (point-cloud.js port)."""
    if not (t >= 0):        # noqa: E501 - clamp; NaN -> 0 (mirrors the JS)
        t = 0.0
    elif t > 1:
        t = 1.0
    t2 = t * t
    t3 = t2 * t
    t4 = t3 * t
    t5 = t4 * t
    r = (0.13572138 + 4.61539260 * t - 42.66032258 * t2 + 132.13108234 * t3
         - 152.94239396 * t4 + 59.28637943 * t5)
    g = (0.09140261 + 2.19418839 * t + 4.84296658 * t2 - 14.18503333 * t3
         + 4.27729857 * t4 + 2.82956604 * t5)
    b = (0.10667330 + 12.64194608 * t - 60.58204836 * t2 + 110.36276771 * t3
         - 89.90310912 * t4 + 27.34824973 * t5)

    def c255(v):
        v = _js_round(v * 255)
        return 0 if v < 0 else (255 if v > 255 else v)

    return (c255(r), c255(g), c255(b))


def _resolve_colors(n, decoded):
    """A 3*n colour bytearray for a decoded cloud without RGB: ramp by
    intensity (turbo) when the LAS decode supplied a varying intensity, else
    a flat mid-grey (pointcloud-import.js port)."""
    if decoded.get("colors") is not None:
        return decoded["colors"]
    intens = decoded.get("intensities")
    if intens is not None and len(intens) == n:
        mn = min(intens) if n else 0
        mx = max(intens) if n else 0
        if mx > mn:
            out = bytearray(3 * n)
            rng = mx - mn
            for i in range(n):
                c = _colormap_turbo((intens[i] - mn) / rng)
                o = 3 * i
                out[o] = c[0]
                out[o + 1] = c[1]
                out[o + 2] = c[2]
            return out
    return bytearray([DEFAULT_GREY]) * (3 * n)


# ==================================================== the toCloud pipeline
_UNSUPPORTED_CRS_MSG = (
    "This file's coordinates don't look like WGS84 latitude/longitude and no "
    "supported UTM zone was detected ({reason}). Reproject the layer to WGS84 "
    "(EPSG:4326) in QGIS first — e.g. right-click the layer > Export with a "
    "different CRS — or publish it with 'Keep local coordinates'."
)


def _refuse_too_large(n):
    raise PointCloudError(
        "This point cloud is too detailed to import ({} points). "
        "Please downsample it first.".format(n), too_large=True)


def _local_cloud(decoded, n):
    """The LOCAL lane: cap, resolve colours, centre on the centroid (best
    float32 precision near the origin). origin=None, bounds=None — an
    identity-placed tileset (PLY always; LAS with placement='local')."""
    if n > PUBLISH_MAX_POINTS:
        _refuse_too_large(n)
    colors = _resolve_colors(n, decoded)
    bounds = decoded.get("bounds")
    if not (isinstance(bounds, dict) and "min" in bounds):
        bounds = _min_max(decoded["positions"], n)
    cloud = {"positions": decoded["positions"], "colors": colors,
             "point_count": n, "bounds": bounds}
    if n > PUBLISH_TARGET_POINTS:
        cloud = _cap_points(cloud, PUBLISH_TARGET_POINTS)

    m = cloud["point_count"]
    src = cloud["positions"]
    sx = sy = sz = 0.0
    for i in range(m):
        p = 3 * i
        sx += src[p]
        sy += src[p + 1]
        sz += src[p + 2]
    cx, cy, cz = sx / m, sy / m, sz / m
    positions = array("f", bytes(12 * m))
    for i in range(m):
        p = 3 * i
        positions[p] = src[p] - cx
        positions[p + 1] = src[p + 1] - cy
        positions[p + 2] = src[p + 2] - cz
    return {
        "cloud": {"positions": positions, "colors": cloud["colors"],
                  "point_count": m},
        "origin": None,
        "bounds": None,
    }


def _georeferenced_cloud(decoded, n, reproject_fn):
    """The REPROJECT lane (LAS): resolve the CRS (or use the injected
    ``reproject_fn``), cap, reproject every surviving point to WGS84, build
    the georef frame at the centroid and convert to LOCAL metric Y-up
    (x=E, y=Up, z=-N) — a faithful ``pointcloud-import.js _fromLas`` port."""
    det = None
    if reproject_fn is None:
        sb = decoded.get("bounds") or {}
        det = detect_crs(decoded.get("crs"), sb)
        if det["kind"] == "unsupported":
            raise PointCloudError(
                _UNSUPPORTED_CRS_MSG.format(
                    reason=det.get("reason") or "unknown projection"),
                unsupported=True, reason=det.get("reason"))

    if n > PUBLISH_MAX_POINTS:
        _refuse_too_large(n)

    colors = _resolve_colors(n, decoded)
    # Downsample a very large cloud BEFORE the per-point transform (capping is
    # CRS-agnostic: a spatial thin on the RAW decoded positions).
    cloud = {"positions": decoded["positions"], "colors": colors,
             "point_count": n,
             "bounds": _min_max(decoded["positions"], n)}
    if n > PUBLISH_TARGET_POINTS:
        cloud = _cap_points(cloud, PUBLISH_TARGET_POINTS)

    m = cloud["point_count"]
    gpos = cloud["positions"]
    geo = array("d", bytes(24 * m))
    if reproject_fn is not None:
        for i in range(m):
            p = 3 * i
            lat, lng = reproject_fn(gpos[p], gpos[p + 1])
            geo[p] = lng
            geo[p + 1] = lat
            geo[p + 2] = gpos[p + 2]
    elif det["kind"] == "utm":
        zone, hemi = det["zone"], det["hemisphere"]
        for i in range(m):
            p = 3 * i
            lat, lng = utm_to_wgs84(gpos[p], gpos[p + 1], zone, hemi)
            geo[p] = lng
            geo[p + 1] = lat
            geo[p + 2] = gpos[p + 2]
    else:  # wgs84 — byte-for-byte pass-through
        for i in range(m):
            p = 3 * i
            geo[p] = gpos[p]
            geo[p + 1] = gpos[p + 1]
            geo[p + 2] = gpos[p + 2]

    # Centroid (origin) + geographic bounds over the effective cloud.
    sum_lng = sum_lat = sum_alt = 0.0
    min_lng = min_lat = math.inf
    max_lng = max_lat = -math.inf
    for i in range(m):
        p = 3 * i
        lng, lat, alt = geo[p], geo[p + 1], geo[p + 2]
        sum_lng += lng
        sum_lat += lat
        sum_alt += alt
        min_lng = min(min_lng, lng); max_lng = max(max_lng, lng)  # noqa: E702
        min_lat = min(min_lat, lat); max_lat = max(max_lat, lat)  # noqa: E702
    origin = {"lat": sum_lat / m, "lng": sum_lng / m, "alt": sum_alt / m}

    # DEFENSE-IN-DEPTH: after reprojection the origin + extent MUST be in
    # valid geographic range — refuse with the SAME clear message rather than
    # let the georef math emit garbage.
    if not (abs(origin["lat"]) <= 90 and abs(origin["lng"]) <= 180
            and abs(min_lat) <= 90 and abs(max_lat) <= 90
            and abs(min_lng) <= 180 and abs(max_lng) <= 180):
        raise PointCloudError(
            _UNSUPPORTED_CRS_MSG.format(
                reason="reprojected coordinates out of range"),
            unsupported=True, reason="reprojected coordinates out of range")

    # Local metric Y-up: x=E, y=Up, z=-N (Float32Array parity with the JS).
    positions = array("f", bytes(12 * m))
    o_lat, o_lng, o_alt = origin["lat"], origin["lng"], origin["alt"]
    for i in range(m):
        p = 3 * i
        e, north, u = _latlng_to_enu(o_lat, o_lng, o_alt,
                                     geo[p + 1], geo[p], geo[p + 2])
        positions[p] = e
        positions[p + 1] = u
        positions[p + 2] = -north

    crs_info = ({"source": "custom"} if det is None else
                {"source": det["kind"], "zone": det.get("zone"),
                 "hemisphere": det.get("hemisphere"), "epsg": det.get("epsg")})
    return {
        "cloud": {"positions": positions, "colors": cloud["colors"],
                  "point_count": m},
        "origin": origin,
        "bounds": [min_lng, min_lat, max_lng, max_lat],
        "crs": crs_info,
    }


def to_cloud(data, filename, placement="reproject", reproject_fn=None):
    """RAW uploaded file bytes -> the tileset-ready cloud (the orchestrator
    ``tiles3d.publish_point_cloud`` calls) — a ``pointcloud-import.js
    toCloud`` port with a QGIS-specific ``placement`` axis:

    * ``placement='reproject'`` (default): a LAS/LAZ is georeferenced — its
      CRS resolved from the VLR hint (WGS84 pass-through / UTM closed-form
      inverse), or EVERY point pushed through the injected
      ``reproject_fn(x, y) -> (lat, lng)`` (the GUI passes a
      QgsCoordinateTransform-backed callable for arbitrary CRSs). An
      unsupported CRS without ``reproject_fn`` raises a friendly error
      naming QGIS reprojection.
    * ``placement='local'``: centre on the centroid, ``origin``/``bounds``
      None — works for ANY CRS, including unsupported ones.

    A PLY has no CRS and always takes the local lane. Returns::

        {cloud: {positions (local Y-up float32), colors, point_count},
         origin: {lat,lng,alt} | None,
         bounds: [min_lng,min_lat,max_lng,max_lat] | None,
         crs?: {source, zone?, hemisphere?, epsg?}}
    """
    if placement not in ("reproject", "local"):
        raise PointCloudError(
            "placement must be 'reproject' or 'local'", malformed=True)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        _malformed("bytes required")
    data = bytes(data)

    # Magic sniff: LAS/LAZ both start with ASCII "LASF"; PLY starts with
    # "ply" + a newline. LAS vs LAZ is decided by the BYTES (the LASzip top
    # bit at offset 104), not the extension.
    is_lasf = data[0:4] == b"LASF"
    is_ply = (len(data) >= 4 and data[0:3] == b"ply"
              and data[3] in (0x0A, 0x0D))
    if is_lasf:
        decoded = read_las(data)   # the .laz ladder runs inside read_las
        n = int(decoded["point_count"])
        if n <= 0:
            _malformed("the LAS file contains no points")
        if placement == "local":
            return _local_cloud(decoded, n)
        return _georeferenced_cloud(decoded, n, reproject_fn)
    if is_ply:
        decoded = read_ply(data)
        n = int(decoded["point_count"])
        if n <= 0:
            _malformed("the PLY file contains no points")
        return _local_cloud(decoded, n)
    raise PointCloudError(
        "Unsupported file — import a LAS (.las), LAZ (.laz) or PLY (.ply) "
        "point cloud.", unsupported=True)
