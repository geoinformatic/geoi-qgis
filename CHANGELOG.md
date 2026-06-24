# Changelog

## 1.1.0 — cloud raster tiling, multi-provider sign-in, AGPL

This release brings the work matured across the `0.8.0`–`0.19.0` development
series to the stable line on top of `1.0.0`.

- **Publish raster as cloud-native tiles.** A new *Publish as Tile Service
  (raster)* flow tiles your **raster layers** or a **folder of GeoTIFFs** into a
  single **PMTiles** archive (WebP + overviews) and publishes it to geoi. Tiles
  are **always Web Mercator (EPSG:3857)** — the CRS is fixed, no override. The
  archive uploads **directly to object storage** via a presigned URL, so your
  bearer token is never sent off-site, with live **progress and a Cancel** in the
  QGIS task manager; a cancelled publish deletes its partial files and never
  registers a half-upload. The **PMTiles writer is bundled** (the pure-Python
  `pmtiles` package is vendored under `geoi/_vendor/`), so it works out of the
  box — a native `pmtiles` CLI is used automatically if present (faster). Needs
  **GDAL ≥ 3.8** (ships with QGIS). Uncovered areas of a reprojected raster are
  **transparent** (no black borders).
- **Sign in with Google, Apple or Microsoft.** The sign-in page offers whichever
  providers your geoi admin enabled, read **live** from the platform (nothing is
  hardcoded). Google and Apple keep the secure loopback handoff; Microsoft uses
  the platform's server-side flow.
- **Storage overview.** The account row shows your total storage usage and
  percentage at a glance, with a per-kind breakdown on hover. Loaded fail-soft,
  so it never slows sign-in.
- **Manage published tile services.** List your tile services, copy their
  XYZ / WMTS / PMTiles URLs into QGIS, add them as **WMTS** layers, and rename,
  change visibility, move, share or delete them. The content tree now groups
  items into **Feature Services / Web Maps / Tile Services** with per-kind icons.
- **Send feedback.** A *Send feedback…* item reports a bug, feature request or
  question to the geoi team — with an optional screenshot/PDF and the relevant
  system info shown before sending. Works signed out.
- **Calmer startup & faster sign-in.** The plugin starts signed-out and idle; a
  saved session is re-established silently (no browser, no prompt) and nothing
  launches a sign-in browser on QGIS start. The provider check no longer blocks
  the UI thread, and the Google loopback capture server stays bound for the whole
  sign-in window (fixes the `ERR_CONNECTION_REFUSED` on redirect).
- **Relicensed under [AGPL-3.0](LICENSE).** The bundled PMTiles writer remains
  third-party BSD-3-Clause code (see [NOTICE](NOTICE)).

## 1.0.0 — first stable release
- First stable release on the QGIS Plugin Repository (`experimental` cleared).
- Connect QGIS to a geoi platform end to end: zero-config Google sign-in,
  browse your own content by folder, add services as native ArcGIS Feature
  Service layers with an editable round-trip, and publish or save projects with
  symbology and labels carried over — with a per-layer summary before you send.
- Licensed under Apache-2.0.

## 0.7.0 — see what you publish
- The **Publish** and **Save to geoi** dialogs now show a per-layer summary —
  the layer name and **how it's styled** (single / categorized / graduated),
  whether it has **labels**, and the **feature count** — so you can see exactly
  what will be carried before you send it.

## 0.6.0 — your map's look comes with it (Save to geoi)
- **Symbology is carried over.** Saving a project to geoi now translates each
  QGIS layer's renderer into the geoi style model: **single symbol**,
  **categorized** (→ per-value colours) and **graduated** (→ class breaks),
  including fill/outline colour, point size, line width and **layer opacity**.
- **Labels too.** A layer's labels (field, font, size, colour, halo) are carried
  over so the saved map labels the same features.
- **Layer names** are preserved.
- Result: a project saved from QGIS renders the same in the geoi web app. (The
  *publish-as-service* path gets the same treatment next — tracked separately.)

## 0.5.1 — load on QGIS 4
- **Fixed "This plugin is incompatible with this version of QGIS" on QGIS 4.**
  The metadata declared only `qgisMinimumVersion=3.22`, so QGIS capped the
  supported range at 3.99 and refused to load on 4.x. Added
  `qgisMaximumVersion=4.99` (plus a guard test) so it installs on QGIS 3 and 4.

## 0.5.0 — proven editing, group sharing, polished panel
- **End-to-end editing proof in CI.** A headless QGIS now adds a live service,
  moves a feature, commits through the ArcGIS provider, and the change is read
  back **straight from the server** and asserted to have persisted — across
  QGIS 3.28 / 3.34 / latest. This locks the 0.4.0 fix as a real round-trip.
- **Share from the tree.** Right-click a service or project → *Share…* to set
  its visibility (private / a group / public) and pick which of your groups it
  is shared with.
- **Quick actions.** *Copy service URL* (paste into ArcGIS / Field Maps) and
  *Open geoi web app*.
- **Polished panel.** Per-kind icons, toolbar icons, alternating rows,
  tidier spacing and expansion — all palette-aware, so it follows QGIS
  light/dark themes. Double-clicking a service adds it (no longer toggles the
  row).

## 0.4.0 — editing that sticks, correct placement, rename-by-click
- **Fixed: edits were lost on save.** A service is now added in its **own
  spatial reference** (the platform serves Web Mercator / EPSG:3857) instead
  of a hardcoded EPSG:4326. The CRS mismatch made QGIS write edited geometry
  under the wrong projection, so committed features landed off-map and
  appeared to vanish. Reads, the layer extent and round-trip editing are now
  all consistent.
- **Fixed: layers only appeared after panning.** Adding a service now repaints
  the canvas and **frames the new layers** (zoom to their combined extent), so
  they draw immediately and the map jumps to the right place.
- **Rename by clicking.** Click a selected service, project or folder (or press
  F2, or use the context menu) to rename it inline.
- Each item now carries a **kind icon** and a tooltip; an empty account shows a
  helpful hint.

## 0.3.0 — content management: visibility, folders, drag-and-drop
- **Publish** now lets you choose the new service's visibility (private,
  shared with a group, or public) and whether its features are **editable**
  — applied right after the upload, in one step.
- The panel shows **only your own** services and projects, never other
  people's public or group-shared content.
- The content tree now mirrors your **folder hierarchy** (sub-folders and
  all).
- **Drag a service or project onto a folder** to move it; right-click for
  *Move to folder…*, and to **create, rename or delete** folders and to
  **delete** a service or project.

## 0.2.2 — fix publish/save (POST was downgraded by a redirect)
- The platform 301-redirects geoi.de -> www.geoi.de; urllib followed it but
  turned the POST into a GET (dropping the body + bearer), so publish/save
  hit the *list* handler and created nothing. The client now follows
  redirects WITHOUT downgrading the method, keeps the bearer on same-site
  redirects, and defaults to the canonical https://www.geoi.de.

## 0.2.1 — reliable publish/save + diagnostics
- Publish/save now VERIFY the server confirmed creation (a service `name` /
  a project `id`); a 2xx that creates nothing now raises a clear error
  instead of silently 'succeeding'.
- New **geoi** channel in the Log Messages panel records every request and
  HTTP status, so issues are diagnosable.
- (QGIS 4) dialogs use `.exec()`; geometry via `int(geometryType())`;
  `messageBar().pushInfo`; `storeAuthenticationConfig`.

## 0.2.0 — one-click, zero-config Google sign-in
- Sign-in now reuses the platform's **own** web Google sign-in: the plugin opens
  the hosted `desktop-signin.html` page (served from the geoi origin, so it uses
  the existing web OAuth client) and receives the session token over a secure
  `127.0.0.1` loopback handoff with a `state` nonce. **No OAuth client, no
  configuration** for the owner or the user.
- Removed the Desktop-OAuth-client requirement and the client-id setting; the
  plugin is no longer a Google client.
- Fallback: paste a one-time code on locked-down machines.

## 0.1.0 — first release
- Google Sign-In via the OAuth 2.0 loopback (PKCE) flow; session token stored
  encrypted in the QGIS authentication database.
- Dockable panel to browse your geoi services, projects and folders.
- Add a service as native ArcGIS Feature Service layer(s); private services
  authenticate via an injected bearer header.
- Publish the current QGIS vector layers as a geoi Feature Service.
- Save the current QGIS vector layers to the platform as a geoi project
  ("geoi package").
- Server settings dialog (platform URL + optional Desktop OAuth client id).
