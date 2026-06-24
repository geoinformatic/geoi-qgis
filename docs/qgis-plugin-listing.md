# geoi — plugins.qgis.org listing (v1.1.0)

Ready-to-paste copy for the plugin's page on
<https://plugins.qgis.org/> (Edit plugin → geoi). It mirrors
`geoi/metadata.txt` so the web GUI and the uploaded ZIP stay in sync. When the
`1.1.0` PR merges to `main`, `.github/workflows/release.yml` automatically tags
`v1.1.0` and publishes a GitHub Release with `geoi.zip` attached — download that
ZIP and update the fields below.

---

## Description

> Sign in to your geoi platform, browse your content, add your services as
> layers, publish or save QGIS projects, and publish raster as cloud-native
> tiles back to geoi.

## About

> The geoi plugin connects QGIS to a geoi platform (geoi.de or your own server).
> Click "Sign in" — it reuses your platform's own web sign-in in your system
> browser (whichever of Google, Apple or Microsoft your admin enabled), with
> zero configuration — then browse your services and projects and double-click a
> service to add it as a native ArcGIS Feature Service layer. Publish the current
> QGIS layers as a new geoi Feature Service, save them to the platform as a geoi
> project ("geoi package"), or publish raster layers / a folder of GeoTIFFs as a
> cloud-native PMTiles tile set (always Web Mercator / EPSG:3857). Tiling needs
> GDAL >= 3.8 (bundled with QGIS); the PMTiles writer is bundled (a native
> pmtiles CLI is used if present). The session token is stored encrypted in the
> QGIS authentication database. Works on QGIS 3 and 4.

## Author

- **Author:** geoi.de (geoinformatic)
- **Author email:** mail@geoi.de
- **Maintainer:** geoi — keep *Display "Created by" in plugin details* checked.

## Tags

```
feature service, arcgis, authentication, cloud, geoi, google, apple, microsoft,
sso, publish, raster, pmtiles, tiles, web, wfs, wms, wmts
```

Changes from the previous listing: **added** `apple`, `microsoft`, `sso`,
`raster`, `pmtiles`, `tiles`, `wmts`. (Drop the old plain `wmts`/`wms`/`wfs`
duplicates if any; the set above is the canonical one.)

## Links

- **Plugin homepage:** <https://github.com/geoinformatic/geoi-qgis#usage>
- **Tracker:** <https://github.com/geoinformatic/geoi-qgis/issues>
- **Code repository:** <https://github.com/geoinformatic/geoi-qgis>

## Flags

- **Experimental:** unchecked (this is a stable `1.1.0` release).
- **Deprecated:** unchecked.
- **Server:** leave unchecked (this is a desktop plugin, not a server plugin).

## Licence

**GNU AGPL-3.0.** The plugin was relicensed from Apache-2.0 to AGPL-3.0 in
`1.1.0`. The bundled PMTiles writer (`geoi/_vendor/pmtiles/`) remains third-party
BSD-3-Clause — see the repository `NOTICE`. The QGIS plugin page has no licence
field; the licence travels in `metadata.txt` (`license=AGPL-3.0`) and `LICENSE`.

## What's new in 1.1.0 (for the release announcement)

- **Publish raster as cloud-native tiles** — tile raster layers or a folder of
  GeoTIFFs into a single PMTiles archive (WebP + overviews, always EPSG:3857),
  uploaded straight to object storage via a presigned URL (your bearer is never
  sent off-site), with progress + cancel. The PMTiles writer is bundled, so it
  works out of the box.
- **Sign in with Google, Apple or Microsoft** — whichever your geoi admin
  enabled, read live from the platform.
- **Storage overview** in the account row.
- **Manage published tile services** — add as WMTS, copy XYZ/WMTS/PMTiles URLs,
  rename, change visibility, move, share, delete; a clearer Feature Services /
  Web Maps / Tile Services tree.
- **Calmer startup** (no forced sign-in) and a robust Google loopback sign-in.
- **Relicensed under AGPL-3.0.**

## Upload checklist

1. Download `geoi.zip` from the **`v1.1.0` GitHub Release** (auto-published when
   the PR merges to `main`; top-level `geoi/` folder with `metadata.txt` at its
   root). Or build it locally with `scripts/package.sh`.
2. *Edit plugin → geoi* → drag in the new ZIP; QGIS reads version `1.1.0`,
   `experimental=False` and the new `about`/`tags` from `metadata.txt`.
3. Paste the **Description**, **About** and **Tags** above if the form does not
   auto-fill them from the ZIP.
4. Confirm **Experimental** and **Deprecated** are unchecked, then save.
