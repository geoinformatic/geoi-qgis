# Security Policy

## Supported versions

Security fixes are released for the latest minor release line only; please
upgrade before reporting.

| Version | Supported |
|---------|-----------|
| 1.7.x   | yes       |
| < 1.7   | no        |

## Reporting a vulnerability

Please report security issues **privately** to **mail@geoi.de** rather than
opening a public issue. Include:

- a description of the issue and its impact,
- steps to reproduce (a proof of concept if possible),
- the plugin version and your QGIS version.

We will acknowledge your report, keep you updated on the fix, and credit you in
the changelog if you wish. Please give us a reasonable window to release a fix
before any public disclosure.

## Design notes

- Sign-in uses the **system browser**, not an embedded webview; each provider's
  sign-in (Google, Apple, Microsoft, ArcGIS) happens on its real domain via the
  platform's existing web flow.
- The token handoff only ever redirects to a validated
  `http://127.0.0.1:<port>` loopback, guarded by a `state` nonce.
- Session tokens are stored **encrypted** in the QGIS authentication database
  (`QgsAuthManager`) and are injected as a request header — never written into a
  layer URL or a log message.
- TLS is verified; no OAuth client ids or secrets are shipped in the plugin.
