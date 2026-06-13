<h1 align="center">geoi — QGIS plugin</h1>

<p align="center">
  Connect QGIS to a <a href="https://geoi.de">geoi</a> platform —
  <strong>sign in with Google</strong>, <strong>browse</strong> your services and
  projects, <strong>add</strong> your services as native layers, and
  <strong>publish</strong> or <strong>save</strong> QGIS projects back to geoi.
</p>

<p align="center">
  <a href="https://github.com/geoinformatic/geoi-qgis/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/geoinformatic/geoi-qgis/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="QGIS 3.22 – 4.x" src="https://img.shields.io/badge/QGIS-3.22%20LTR%20%E2%86%92%204.x-589632.svg">
  <img alt="Dependency-free" src="https://img.shields.io/badge/dependencies-none-brightgreen.svg">
</p>

---

The plugin is intentionally small, **dependency-free** (Python standard library +
the QGIS API only), high-performance (native ArcGIS Feature Service provider +
background tasks), and works across QGIS versions (**3.22 LTR → latest, Qt5 and
Qt6**).

## Features

- **Google Sign-In — one click, zero configuration.** The plugin reuses your
  geoi platform's **own** Google sign-in: click *Sign in with Google* and your
  system browser opens the same Google button the web app shows; pick your
  account and you're in. No OAuth client to create, no settings. The 30-day
  session token is stored **encrypted** in the QGIS authentication database
  (`QgsAuthManager`), never in plain text.
- **Browse your own content** (services, projects) in a dockable panel,
  organised by your **folder hierarchy** (sub-folders and all). You only ever
  see content you own — never other people's public or group-shared items.
- **Add a service** — double-click → it's added via QGIS's ArcGIS Feature
  Service provider (server-side query / paging / BBOX; nothing is bulk-downloaded),
  **in the service's own CRS** so editing round-trips correctly and the map
  **frames the new layers** immediately. Private services authenticate
  automatically via an injected `Authorization: Bearer` header. An **editable**
  service can be edited in QGIS and the changes are saved back to geoi.
- **Rename by clicking** — click a selected service, project or folder (or press
  F2) to rename it inline.
- **Organise** — drag a service or project onto a folder to move it; right-click
  to *Move to folder…*, to **create / rename / delete** folders, and to
  **delete** a service or project.
- **Share** — right-click → *Share…* to set a service's or project's visibility
  (private / a group / public) and pick which of your groups it's shared with.
  *Copy service URL* hands the FeatureServer URL to ArcGIS / Field Maps.
- **Publish a project** as a geoi Feature Service, choosing its **visibility**
  (private, shared with a group, or public) and whether its features are
  **editable**.
- **Save a project** to the platform as a geoi project / "geoi package" —
  re-opens in the geoi web app and in this plugin. Your map's **symbology**
  (single / categorized / graduated), **labels**, opacity, outline and layer
  names are carried over, so it renders the same in the web app.

## Install

**From the QGIS Plugin Repository** *(recommended)*: in QGIS open
*Plugins → Manage and Install Plugins…* and search for **“geoi”**.

**Manually:** download the latest `geoi.zip` from
[Releases](https://github.com/geoinformatic/geoi-qgis/releases), or build it
yourself with `scripts/package.sh`, then use *Plugins → Manage and Install
Plugins → Install from ZIP*.

After installing, click the **geoi** toolbar button to open the panel.

## Configuration

**None.** Sign-in reuses the platform's existing web Google sign-in, so there is
nothing for the platform owner or the user to set up. The only optional setting
is the **Platform URL** (defaults to `https://geoi.de`) if you run your own geoi
server.

## How sign-in works (zero-config handoff)

The plugin is **not** a Google OAuth client. It opens the platform's hosted
handoff page `…/desktop-signin.html` in your system browser. Because that page is
served from the geoi origin, it reuses the **same** web Google client and the
**same** token exchange the web app uses. The page then hands the resulting
session token back to the plugin over a **loopback** redirect
(`http://127.0.0.1:<port>`) — and only a loopback; a `state` nonce ties the
response to the request. On locked-down machines the page also shows a one-time
code you can paste into QGIS instead.

> This plugin talks to a geoi server but ships **no** server code. The handoff
> page and the REST API live on your geoi platform.

## How it talks to geoi (no surprises)

All calls go to `<platform>/platform/…`, exactly like the geoi web app:

| Action | Request |
|---|---|
| Sign in | browser → `…/desktop-signin.html` (reuses the web Google sign-in) → 30-day bearer via loopback |
| Browse | `GET /hub/services`, `/hub/projects`, `/hub/folders`, `/hub/groups` |
| Add service | `…/rest/services/<name>/FeatureServer` via the ArcGIS provider |
| Publish | `POST /hub/services` (multipart GeoJSON), then visibility / editable + group share |
| Save project | `POST /hub/projects {name, data}` |
| Move | `POST /hub/services/<name> {folderId}` · `POST /hub/projects/<id> {folderId}` |
| Folders | `POST /hub/folders` · `POST /hub/folders/<id>` · `DELETE /hub/folders/<id>` |
| Delete | `DELETE /hub/services/<name>` · `DELETE /hub/projects/<id>` |

The Feature Service REST surface is locked by the platform's spec-conformance
gate, so it won't silently change under the plugin.

## Security

- Sign-in uses the **system browser** (not an embedded webview); Google sign-in
  happens on Google's real domain via the platform's existing web client.
- The handoff page **only** redirects to a validated `http://127.0.0.1:<port>`
  loopback — never to a free-form URL — and a `state` nonce prevents any other
  local process from injecting a token.
- Tokens live in the encrypted QGIS auth DB; the bearer is injected as a request
  header for layers (never placed in a layer URL or logged).
- TLS is verified; no client ids or secrets are shipped in the plugin.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Develop

```bash
# unit tests (pure modules — no QGIS needed)
python3 -m unittest discover -s tests -p 'test_*.py'

# editing round-trip: headless QGIS <-> a live platform (needs php + qgis python)
bash tests/e2e/run.sh

# build the installable zip
scripts/package.sh        # → dist/geoi.zip
```

The pure modules (`geoi_client.py`, `convert.py`, `auth.py`, `content_tree.py`)
import nothing from QGIS and carry the testable logic; QGIS is imported lazily
inside the GUI / adapter code so the package imports cleanly in CI.

`tests/e2e/` seeds a public, editable feature service, serves the platform over
real HTTP (`router.php` mirrors the production rewrites), then drives a headless
QGIS to add the layer **in the service's own CRS**, move a feature, commit, and
verify the edit persisted on the server. It runs in CI across QGIS 3.28 / 3.34 /
latest.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and our
[Code of Conduct](CODE_OF_CONDUCT.md).

## About geoi

geoi is a mobile-first WebGIS platform for capturing, sharing and publishing
geodata — in the browser, on your phone, and (with this plugin) right inside
QGIS. If you don't have an account yet, you're welcome to take a look and try it
out at **[geoi.de](https://geoi.de)**. You can also point the plugin at your own
geoi server.

## License

[Apache License 2.0](LICENSE) © geoinformatic. "geoi" is a product name and
trademark of geoinformatic; the Apache-2.0 grant covers the source code, not the
name or logo.
