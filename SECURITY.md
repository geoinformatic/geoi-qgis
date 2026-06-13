# Security Policy

## Supported versions

The latest released version of the plugin receives security fixes.

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

- Sign-in uses the **system browser**, not an embedded webview; Google sign-in
  happens on Google's real domain via the platform's existing web client.
- The token handoff only ever redirects to a validated
  `http://127.0.0.1:<port>` loopback, guarded by a `state` nonce.
- Session tokens are stored **encrypted** in the QGIS authentication database
  (`QgsAuthManager`) and are injected as a request header — never written into a
  layer URL or a log message.
- TLS is verified; no OAuth client ids or secrets are shipped in the plugin.
