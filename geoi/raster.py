"""Raster -> PMTiles -> presigned-upload pipeline (#552, Part A / A5).

Turn a directory or a set of GeoTIFFs into a single cloud-native
**PMTiles** archive in **Web Mercator (EPSG:3857)** and push it to the
geoi platform's object store via a *presigned* upload URL.

EPSG:3857 is **forced**: there is no CRS parameter, no override, no
setting. Web-map tiles are Web Mercator by definition; warping to any
other CRS would produce tiles the geoi web client cannot consume, so the
target CRS is a module constant and ``assert_target_crs`` re-checks the
warp output before it is ever tiled or uploaded.

GDAL is imported lazily inside the functions (mirroring ``convert.py``),
so this module imports cleanly off a GDAL install — only the functions
that actually do raster work require it.

Pipeline:
  build_vrt(paths)        mosaic the input TIFFs into one virtual raster
  warp_to_3857(vrt)       reproject to EPSG:3857 (bilinear, multithreaded)
  assert_target_crs(out)  re-open and prove the SRS is EPSG:3857, else raise
  to_pmtiles(tif)         WebP MBTiles + overviews -> PMTiles
  publish(client, …)      presign -> PUT to the store -> confirm -> publicUrl
"""

import os

# The ONLY target CRS. Web-map tiles are Web Mercator; this is not a
# parameter and there is no override anywhere in this module.
TARGET_CRS = "EPSG:3857"


def _gdal():
    """Import GDAL lazily and surface a clear error when it is missing."""
    try:
        from osgeo import gdal
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RasterError(
            "GDAL (the `osgeo` Python bindings) is required to build raster "
            "tiles. It ships with QGIS; if you are running outside QGIS, "
            "install GDAL >= 3.8."
        ) from exc
    gdal.UseExceptions()
    return gdal


class RasterError(Exception):
    """A raster-pipeline error with an actionable, user-facing message."""


class RasterCancelled(Exception):
    """Raised when the user cancels the publish pipeline mid-flight.

    Distinct from ``RasterError`` so the task can clean up partial output and
    show "Publishing cancelled" rather than a failure message — and so a cancel
    NEVER confirms/registers a half-upload server-side.
    """


def _check_cancel(is_cancelled):
    """Raise ``RasterCancelled`` if the host signalled a cancel. A no-op when
    ``is_cancelled`` is None (the pure-test / non-task path)."""
    if is_cancelled and is_cancelled():
        raise RasterCancelled("Publishing cancelled.")


def _report(progress, value):
    """Call the per-stage progress callback (0..100) fail-soft."""
    if progress:
        try:
            progress(value)
        except Exception:  # noqa: BLE001 - progress must never break the run
            pass


# --------------------------------------------------------------- inputs
def collect_tiffs(paths):
    """Expand ``paths`` (files and/or directories) to a sorted TIFF list.

    A directory is walked one level deep for ``*.tif`` / ``*.tiff``. Files
    are taken as-is. Order is deterministic so a mosaic is reproducible.
    """
    out = []
    for path in paths or []:
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.lower().endswith((".tif", ".tiff")):
                    out.append(os.path.join(path, name))
        elif path:
            out.append(path)
    # de-dup while keeping order
    seen = set()
    unique = []
    for path in out:
        key = os.path.abspath(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


# ---------------------------------------------------------------- build
def build_vrt(paths, vrt_path=None, is_cancelled=None):
    """Mosaic ``paths`` (a directory, a file, or a list of either) into a VRT.

    Returns the VRT path. A single input still goes through a VRT so the
    rest of the pipeline has one entry point.
    """
    _check_cancel(is_cancelled)
    gdal = _gdal()
    tiffs = collect_tiffs(paths if isinstance(paths, (list, tuple)) else [paths])
    if not tiffs:
        raise RasterError("No GeoTIFF (.tif/.tiff) inputs were found to tile.")
    vrt_path = vrt_path or _sibling(tiffs[0], ".geoi-mosaic.vrt")
    vrt = gdal.BuildVRT(vrt_path, list(tiffs))
    if vrt is None:
        raise RasterError("GDAL could not mosaic the input rasters into a VRT.")
    # Flush the dataset to disk so downstream gdal.Warp can re-open it.
    vrt.FlushCache()
    vrt = None
    return vrt_path


def warp_to_3857(vrt_path, tif_3857=None, is_cancelled=None, progress=None):
    """Reproject ``vrt_path`` to EPSG:3857. No CRS parameter — it is forced.

    Uses bilinear resampling and multithreaded warping. An alpha band is
    ADDED (``dstAlpha=True``) so the area the rotated/reprojected source
    does NOT cover stays TRANSPARENT (alpha 0) rather than filling with
    opaque black — that black wedge around the data was #680. The output
    is re-checked with ``assert_target_crs`` before it is returned.

    ``progress`` (if given) is fed GDAL's 0..1 warp fraction scaled to 0..100;
    ``is_cancelled`` is polled by GDAL's callback so a long warp can be
    interrupted (the GDAL progress callback returns 0 to abort).
    """
    _check_cancel(is_cancelled)
    gdal = _gdal()
    tif_3857 = tif_3857 or _sibling(vrt_path, ".3857.tif")

    def _cb(fraction, _msg, _data):
        _report(progress, max(0, min(100, int(fraction * 100))))
        # Returning 0/False aborts the GDAL operation; non-zero continues.
        return 0 if (is_cancelled and is_cancelled()) else 1

    warped = gdal.Warp(
        tif_3857,
        vrt_path,
        dstSRS=TARGET_CRS,  # the constant — never a caller-supplied CRS
        resampleAlg="bilinear",
        multithread=True,
        # Add an alpha band: uncovered/no-data pixels become TRANSPARENT,
        # not black. Carried through to the WebP tiles below (#680).
        dstAlpha=True,
        callback=_cb,
    )
    if warped is None:
        # A None return after a user cancel is an abort, not a failure.
        _check_cancel(is_cancelled)
        raise RasterError("GDAL could not reproject the raster to Web Mercator.")
    warped.FlushCache()
    warped = None
    _check_cancel(is_cancelled)
    assert_target_crs(tif_3857)
    return tif_3857


def assert_target_crs(dataset_or_path):
    """Re-open the output and RAISE unless its SRS authority is EPSG:3857.

    Accepts a path or an open GDAL dataset. This is the hard guard that
    keeps a non-3857 raster from ever being tiled or uploaded.
    """
    gdal = _gdal()
    opened_here = False
    dataset = dataset_or_path
    if isinstance(dataset_or_path, str):
        dataset = gdal.Open(dataset_or_path)
        opened_here = True
    if dataset is None:
        raise RasterError("Could not open the reprojected raster to verify its CRS.")
    try:
        from osgeo import osr

        wkt = dataset.GetProjection()
        srs = osr.SpatialReference()
        if wkt:
            srs.ImportFromWkt(wkt)
        authority = srs.GetAuthorityName(None)
        code = srs.GetAuthorityCode(None)
    finally:
        if opened_here:
            dataset = None
    epsg = "{}:{}".format(authority, code) if authority and code else None
    if epsg != TARGET_CRS:
        raise RasterError(
            "Reprojection check failed: the output CRS is {} but must be {}. "
            "Refusing to tile a non-Web-Mercator raster.".format(
                epsg or "unknown", TARGET_CRS
            )
        )
    return True


# -------------------------------------------------------------- PMTiles
def _pmtiles_writer():
    """Detect the MBTiles->PMTiles converter to use at runtime.

    Preference order (fastest / least surprising first):
      1. ``("cli", path)``    — a native ``pmtiles`` CLI on PATH (fastest).
      2. ``("python", "system")``   — a user-installed ``pmtiles`` package.
      3. ``("python", "vendored")`` — the pure-Python ``pmtiles`` bundled
         under ``geoi/_vendor`` (ALWAYS available, so this never returns
         ``(None, None)`` — raster->PMTiles works out of the box).
    """
    import shutil

    cli = shutil.which("pmtiles")
    if cli:
        return "cli", cli
    try:
        import pmtiles.convert  # noqa: F401

        return "python", "system"
    except ImportError:
        # The bundled copy is the guaranteed floor.
        return "python", "vendored"


def to_pmtiles(tif_3857, pmtiles_path=None, is_cancelled=None, progress=None):
    """Build a WebP PMTiles archive (with overview pyramids) from a 3857 TIFF.

    Toolchain (detected at runtime):
      1. GDAL (>= 3.8) writes a WebP-tiled MBTiles via ``gdal.Translate``,
         then ``gdal.BuildOverviews`` adds the overview pyramid;
      2. the MBTiles is converted to PMTiles by the native ``pmtiles`` CLI
         if it is on PATH, else by the pure-Python ``pmtiles`` package —
         the bundled ``geoi/_vendor`` copy if none is installed, so this
         works out of the box.

    The PMTiles writer is always bundled, so this NEVER silently emits a
    non-PMTiles file.
    """
    _check_cancel(is_cancelled)
    gdal = _gdal()
    # Verify the input really is Web Mercator before tiling (defence in depth).
    assert_target_crs(tif_3857)

    kind, writer = _pmtiles_writer()
    if kind is None:
        # Defensive only: _pmtiles_writer always returns a writer (the
        # vendored floor), so reaching here is an internal error.
        raise RasterError(
            "Internal error: no PMTiles writer was resolved although one is "
            "bundled. Please report this."
        )

    pmtiles_path = pmtiles_path or _sibling(tif_3857, ".pmtiles", strip=".3857.tif")
    mbtiles_path = _sibling(tif_3857, ".mbtiles", strip=".3857.tif")

    def _tile_cb(fraction, _msg, _data):
        # Translate fills 0..70% of this stage, leaving headroom for overviews
        # + the PMTiles convert below.
        _report(progress, max(0, min(70, int(fraction * 70))))
        return 0 if (is_cancelled and is_cancelled()) else 1

    # 1) WebP-tiled MBTiles. TILE_FORMAT=WEBP needs GDAL >= 3.8 with WebP.
    #    The 3857 TIFF carries the alpha band added by warp_to_3857; WebP
    #    only PRESERVES that alpha when it encodes LOSSLESS (QUALITY=100 in
    #    the MBTiles driver selects lossless WebP, which keeps the 4th
    #    band). Lossy WebP in this driver discards alpha, which would bring
    #    the black background straight back (#680). So we force lossless.
    # The intermediate MBTiles is always removed on the way out — success,
    # error, OR a mid-pipeline cancel (which raises RasterCancelled before
    # step 3) — so a cancelled publish never leaves a large temp behind.
    translated = None
    try:
        translated = gdal.Translate(
            mbtiles_path,
            tif_3857,
            format="MBTiles",
            creationOptions=["TILE_FORMAT=WEBP", "QUALITY=100"],
            callback=_tile_cb,
        )
        if translated is None:
            _check_cancel(is_cancelled)
            raise RasterError(
                "GDAL could not write a WebP MBTiles. This needs GDAL >= 3.8 "
                "built with WebP support."
            )
        _check_cancel(is_cancelled)
        # 2) overview pyramids so the PMTiles serves every zoom level.
        translated.BuildOverviews("average", [2, 4, 8, 16, 32])
        translated.FlushCache()
        translated = None
        _report(progress, 85)

        # 3) MBTiles -> PMTiles via whichever converter was detected.
        _check_cancel(is_cancelled)
        if kind == "cli":
            _mbtiles_to_pmtiles_cli(writer, mbtiles_path, pmtiles_path)
        else:
            _mbtiles_to_pmtiles_python(mbtiles_path, pmtiles_path)
    finally:
        translated = None
        try:
            os.remove(mbtiles_path)
        except OSError:
            pass

    if not os.path.exists(pmtiles_path):
        raise RasterError("PMTiles conversion produced no output file.")
    _report(progress, 100)
    return pmtiles_path


def _mbtiles_to_pmtiles_cli(cli, mbtiles_path, pmtiles_path):
    import subprocess

    try:
        subprocess.run(
            [cli, "convert", mbtiles_path, pmtiles_path],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - env
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise RasterError(
            "The pmtiles CLI failed to convert MBTiles to PMTiles: " + (detail or str(exc))
        ) from exc


def _vendored_pmtiles_convert(force_vendored=False):
    """Resolve the ``mbtiles_to_pmtiles`` callable without clobbering a
    user's install.

    Prefers a system/user ``pmtiles`` if importable; otherwise falls back
    to the bundled copy under ``geoi/_vendor`` by adding that dir to
    ``sys.path`` TEMPORARILY (restored in ``finally``). Pass
    ``force_vendored=True`` to bypass a (possibly stale) system copy and
    load the bundled module explicitly.
    """
    import importlib
    import sys

    if not force_vendored:
        try:
            import pmtiles.convert as _c  # system/user copy

            return _c.mbtiles_to_pmtiles
        except ImportError:
            pass

    vendor_dir = os.path.join(os.path.dirname(__file__), "_vendor")
    sys.path.insert(0, vendor_dir)
    try:
        # Drop any already-imported system pmtiles so the vendor dir wins.
        for name in [m for m in sys.modules if m == "pmtiles" or m.startswith("pmtiles.")]:
            del sys.modules[name]
        module = importlib.import_module("pmtiles.convert")
        return module.mbtiles_to_pmtiles
    finally:
        try:
            sys.path.remove(vendor_dir)
        except ValueError:  # pragma: no cover - defensive
            pass


def _mbtiles_to_pmtiles_python(mbtiles_path, pmtiles_path):
    # 3 args: pmtiles 3.7.x requires (input, output, maxzoom); None keeps
    # the MBTiles' own maxzoom.
    convert = _vendored_pmtiles_convert()
    try:
        convert(mbtiles_path, pmtiles_path, None)
    except TypeError:
        # A stale (pre-3.7) user `pmtiles` has a different arity. Retry
        # with the bundled 3.7.0 module explicitly, bypassing it.
        convert = _vendored_pmtiles_convert(force_vendored=True)
        convert(mbtiles_path, pmtiles_path, None)


# --------------------------------------------------------------- publish
def raster_bounds(tif_3857):
    """The WGS84 [west, south, east, north] bounds of the 3857 raster.

    Sent to the platform on confirm so the catalogue knows where the tiles
    sit. Best-effort: returns ``None`` if the extent cannot be read.
    """
    gdal = _gdal()
    dataset = gdal.Open(tif_3857)
    if dataset is None:
        return None
    try:
        gt = dataset.GetGeoTransform()
        width, height = dataset.RasterXSize, dataset.RasterYSize
        if not gt or not width or not height:
            return None
        xs = [gt[0], gt[0] + gt[1] * width]
        ys = [gt[3], gt[3] + gt[5] * height]
        from osgeo import osr

        src = osr.SpatialReference()
        src.ImportFromEPSG(3857)
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(4326)
        # Keep traditional lon/lat axis order across GDAL 3 (PROJ 6+).
        try:
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:  # pragma: no cover - very old GDAL
            pass
        ct = osr.CoordinateTransformation(src, dst)
        corners = [ct.TransformPoint(x, y)[:2] for x in xs for y in ys]
        lons = [c[0] for c in corners]
        lats = [c[1] for c in corners]
        return [min(lons), min(lats), max(lons), max(lats)]
    except Exception:  # noqa: BLE001 - bounds are best-effort metadata
        return None
    finally:
        dataset = None


def publish(client, pmtiles_path, name, bounds=None, is_cancelled=None,
            progress=None):
    """Upload a finished ``.pmtiles`` to the platform via a presigned URL.

    1. stat the file size;
    2. ``client.raster_presign(name, bytes)`` -> ``{uploadUrl, objectKey, …}``;
    3. ``client.put_url(uploadUrl, pmtiles_path, 'application/octet-stream')``
       — a direct PUT to the object store that does NOT carry the geoi
       bearer (the presigned URL self-authenticates);
    4. ``client.raster_confirm(objectKey, bytes, bounds)`` finalises the
       catalogue entry;
    5. return the public read URL.

    A cancel is honoured BEFORE the PUT and BEFORE confirm, so a cancelled
    publish NEVER confirms/registers a half-upload server-side.
    """
    if not os.path.exists(pmtiles_path):
        raise RasterError("The PMTiles file to publish does not exist.")
    size = os.path.getsize(pmtiles_path)
    if size <= 0:
        raise RasterError("The PMTiles file is empty.")

    _check_cancel(is_cancelled)
    presign = client.raster_presign(name, size)
    upload_url = presign.get("uploadUrl")
    object_key = presign.get("objectKey")
    if not upload_url or not object_key:
        raise RasterError(
            "The server did not return an upload URL for the raster tiles."
        )

    # Don't even start the PUT if the user already cancelled.
    _check_cancel(is_cancelled)
    _report(progress, 5)
    client.put_url(upload_url, pmtiles_path, "application/octet-stream")
    _report(progress, 95)

    # Crucial: a cancel after the upload but before confirm must NOT finalise
    # the catalogue entry — leave the orphaned object unconfirmed.
    _check_cancel(is_cancelled)
    confirmed = client.raster_confirm(object_key, size, bounds)
    public_url = (
        confirmed.get("publicUrl")
        or confirmed.get("url")
        or presign.get("publicUrl")
    )
    if not public_url:
        raise RasterError(
            "The server accepted the upload but did not return a public tile URL."
        )
    _report(progress, 100)
    return public_url


# ----------------------------------------------------------------- helpers
def cleanup_temp(paths):
    """Delete intermediate/partial pipeline files, fail-soft.

    Called on cancel OR error so a stopped publish leaves no half-written
    ``.vrt`` / ``.3857.tif`` / ``.mbtiles`` / ``.pmtiles`` on disk. ``None``
    and missing paths are ignored.
    """
    for path in paths or []:
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _sibling(path, suffix, strip=None):
    """A sibling path next to ``path`` with ``suffix`` (replacing ``strip``)."""
    base = path
    if strip and base.endswith(strip):
        base = base[: -len(strip)]
    else:
        base = os.path.splitext(base)[0]
    return base + suffix
