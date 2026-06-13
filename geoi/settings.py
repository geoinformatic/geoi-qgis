"""Plugin preferences, stored in QSettings (non-secret values only).

The session *token* is NOT kept here — it lives encrypted in the QGIS
authentication database via ``auth.SessionStore``.
"""

import json

from qgis.PyQt.QtCore import QSettings

_ORG = "geoi"
_APP = "geoi-qgis"
DEFAULT_BASE_URL = "https://www.geoi.de"


def _s():
    return QSettings(_ORG, _APP)


def base_url():
    return _s().value("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL


def set_base_url(value):
    _s().setValue("base_url", (value or DEFAULT_BASE_URL).strip())


def header_authcfg():
    return _s().value("header_authcfg", "") or ""


def set_header_authcfg(value):
    _s().setValue("header_authcfg", value or "")


def session_user():
    raw = _s().value("session_user", "") or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def set_session_user(user):
    _s().setValue("session_user", json.dumps(user) if user else "")
