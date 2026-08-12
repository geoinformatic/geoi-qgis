# Screenshot manifest

| File | What it shows |
|---|---|
| `qgis-panel-offscreen.png` | The plugin's real dock panel (`GeoiPanel` from `geoi/gui/browser_panel.py`) rendered offscreen with plain PyQt5 (`QT_QPA_PLATFORM=offscreen`, `widget.grab()`) and a minimal `qgis`-API shim — no running QGIS instance. It shows the honest signed-out default state: account row with Sign in, My content / Discover switch, empty content tree, and the Publish / Manage actions disabled until sign-in. Inside QGIS the same widget docks into the main window and uses QGIS theming. |

Captured 2026-08-11. Real widget code, no mockups; the panel was not
populated with any data because that would require a signed-in session.

- `plugin-panel.png` — copy of `qgis-panel-offscreen.png` under the name the README references.
- `web-workspace.jpg` — the geoi web workspace (live www.geoi.de, signed out), captured with headless Chromium; the platform this plugin publishes to.
