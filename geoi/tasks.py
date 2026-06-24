"""Background tasks (QgsTask) so the GUI never blocks on the network/OAuth.

Each task runs its work in ``run()`` (worker thread) and calls a Python
``on_done(ok, payload_or_error)`` callback in ``finished()`` (main thread).
"""

from qgis.core import QgsTask

from . import auth as auth_mod
from .geoi_client import GeoiError, friendly_error

# Sentinel prefix the SignInTask uses to mark a DEFINITIVE "sign-in is not
# enabled on this server" verdict (vs a transient failure that should offer the
# paste-code fallback). The controller strips the prefix to show the message.
SIGNIN_DISABLED = "\x00signin-disabled\x00"


class _BaseTask(QgsTask):
    def __init__(self, description, on_done):
        # Default QgsTask flags already allow cancellation; passing the flag
        # explicitly is avoided because the unscoped enum (QgsTask.CanCancel)
        # is unavailable on Qt6/PyQt6 builds.
        super().__init__(description)
        self._on_done = on_done
        self._result = None
        self._error = None

    def work(self):  # override
        raise NotImplementedError

    def run(self):
        try:
            self._result = self.work()
            return True
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self._error = str(getattr(exc, "message", None) or exc)
            return False

    def finished(self, ok):
        if self._on_done is None:
            return
        if ok:
            self._on_done(True, self._result)
        else:
            self._on_done(False, self._error or "Cancelled.")


class SignInTask(_BaseTask):
    """Zero-config sign-in: reuse the web sign-in (the provider chosen on the
    hosted page) via a loopback handoff.

    The provider availability pre-check runs HERE (worker thread), not on the
    UI thread, so clicking "Sign in" opens the browser promptly even if
    ``/auth/providers`` is slow. It uses a SHORT timeout and FAILS OPEN: a
    timeout/transient error does NOT block the browser (the hosted page renders
    the live provider choice and reports "sign-in disabled" itself). Only a
    DEFINITIVE empty provider list short-circuits with the SIGNIN_DISABLED
    verdict, so we never open a browser to a page that can't sign anyone in.
    """

    # Short, non-blocking availability pre-check timeout (seconds).
    PROVIDER_TIMEOUT = 4

    def __init__(self, client, on_done):
        super().__init__("geoi: signing in", on_done)
        self._client = client

    def work(self):
        # DEFINITIVE empty list ⇒ sign-in is off; surface it without opening a
        # useless browser tab. Any error/timeout fails OPEN (assume enabled),
        # so a slow endpoint never blocks the common case.
        try:
            providers = self._client.auth_providers(timeout=self.PROVIDER_TIMEOUT)
            if providers == []:
                raise _SigninDisabled("Sign-in is not enabled on this geoi server.")
        except _SigninDisabled:
            raise
        except Exception:  # noqa: BLE001 - fail open: never block the browser
            pass

        token = auth_mod.run_web_signin(
            self._client.base_url, is_cancelled=self.isCanceled
        )
        self._client.set_token(token)
        user = self._client.me().get("user", {})
        return {"token": token, "user": user}

    def run(self):
        # A definitive "sign-in disabled" verdict is reported to the controller
        # via the SIGNIN_DISABLED-prefixed error so it shows a plain message
        # rather than the paste-code fallback. Everything else behaves like the
        # base task (a failure offers paste).
        try:
            self._result = self.work()
            return True
        except _SigninDisabled as exc:
            self._error = SIGNIN_DISABLED + str(exc)
            return False
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self._error = str(getattr(exc, "message", None) or exc)
            return False


class _SigninDisabled(Exception):
    """Internal: a definitive 'no providers configured' verdict."""


class CatalogTask(_BaseTask):
    """Fetch services + projects + folders in one go.

    ``services`` / ``projects`` / ``folders`` are the CORE (must-succeed)
    content load — the feature-services list must always render. ``groups``
    and ``tiles`` are OPTIONAL context: they are fetched fail-soft AND with a
    SHORT timeout, so a slow or missing ``/hub/groups`` / ``/raster/services``
    endpoint degrades to an empty bucket fast instead of stalling sign-in for
    up to the full client timeout.
    """

    # Optional-call timeout (seconds). Short enough that a hung endpoint can't
    # make sign-in feel slow; the core load keeps the full client timeout.
    OPTIONAL_TIMEOUT = 5

    def __init__(self, client, on_done):
        super().__init__("geoi: loading content", on_done)
        self._client = client

    def work(self):
        # Core content first — these decide whether sign-in "worked", so they
        # keep the full timeout and any error propagates to the UI.
        core = {
            "services": self._client.list_services(),
            "projects": self._client.list_projects(),
            "folders": self._client.list_folders(),
        }
        groups = []
        try:
            groups = self._client.list_groups(timeout=self.OPTIONAL_TIMEOUT)
        except Exception:  # noqa: BLE001 - groups are optional context
            groups = []
        tiles = []
        try:
            tiles = self._client.tile_services(timeout=self.OPTIONAL_TIMEOUT)
        except Exception:  # noqa: BLE001 - tile services may be off / slow
            tiles = []
        # Storage overview (#677) — OPTIONAL context, same fail-soft + short
        # timeout rule: a missing/slow /hub/storage just hides the line, it
        # must never stall sign-in or the content load.
        storage = None
        try:
            storage = self._client.storage(timeout=self.OPTIONAL_TIMEOUT)
        except Exception:  # noqa: BLE001 - storage is optional context
            storage = None
        core["groups"] = groups
        core["tiles"] = tiles
        core["storage"] = storage
        return core


class PublishTask(_BaseTask):
    def __init__(self, client, filename, data, content_type, options, on_done):
        super().__init__("geoi: publishing service", on_done)
        self._client = client
        self._filename = filename
        self._data = data
        self._ctype = content_type
        self._options = options or {}

    def work(self):
        return self._client.publish_service_with_options(
            self._filename, self._data, content_type=self._ctype,
            visibility=self._options.get("visibility"),
            editable=self._options.get("editable"),
            group_ids=self._options.get("group_ids"),
        )


class RasterPublishCancelled(Exception):
    """Internal: the raster pipeline was cancelled by the user."""


class RasterPublishTask(_BaseTask):
    """Run the whole raster -> PMTiles -> presigned-upload pipeline off the
    GUI thread, reporting MEANINGFUL progress and honouring CANCEL.

    Progress (``QgsTask.setProgress``) advances the task-manager bar across the
    real pipeline stages, each stage given a weight that reflects its cost:

      build VRT          0 →  5
      warp → 3857        5 → 40   (the heaviest stage)
      tile → PMTiles    40 → 85
      upload + confirm  85 → 100

    Cancel (``QgsTask.isCanceled``) is honoured at every stage boundary and,
    where GDAL supports a progress callback, MID-stage. On cancel the pipeline
    aborts cleanly, every partial/temporary file is deleted, and NOTHING is
    confirmed/registered server-side — the user sees "Publishing cancelled".

    EPSG:3857 is forced inside ``raster`` — this task passes NO CRS.
    """

    def __init__(self, client, inputs, name, on_done):
        super().__init__("geoi: publishing raster tiles", on_done)
        self._client = client
        self._inputs = inputs
        self._name = name
        # Every intermediate produced, so a cancel/error can wipe partials.
        self._temps = []

    def run(self):
        # Same flow as the base task, but a raster-publish GeoiError is
        # translated to a clear, actionable sentence (e.g. "ask your admin to
        # enable tile services") instead of the cryptic server message. An
        # unmapped code falls back to the message, so other tasks are unchanged.
        # A cancel cleans up partials and reports the cancel verdict.
        from . import raster

        try:
            self._result = self.work()
            return True
        except (RasterPublishCancelled, raster.RasterCancelled):
            self._cleanup()
            self._error = "Publishing cancelled."
            return False
        except GeoiError as exc:
            self._cleanup()
            self._error = friendly_error(exc)
            return False
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self._cleanup()
            self._error = str(getattr(exc, "message", None) or exc)
            return False

    def _cleanup(self):
        from . import raster

        raster.cleanup_temp(self._temps)

    def _scaled(self, lo, hi):
        """A 0..100 stage-progress callback mapped into the [lo, hi] band."""
        def cb(pct):
            self.setProgress(lo + (hi - lo) * max(0, min(100, pct)) / 100.0)
        return cb

    def _guard(self):
        """Stage-boundary cancel check (raises so ``run`` cleans up)."""
        if self.isCanceled():
            raise RasterPublishCancelled()

    def work(self):
        from . import raster

        cancel = self.isCanceled

        self.setProgress(0)
        self._guard()
        vrt = raster.build_vrt(self._inputs, is_cancelled=cancel)
        self._temps.append(vrt)
        self.setProgress(5)

        self._guard()
        # forced EPSG:3857, no CRS parameter
        tif = raster.warp_to_3857(
            vrt, is_cancelled=cancel, progress=self._scaled(5, 40))
        self._temps.append(tif)
        self.setProgress(40)

        self._guard()
        pmtiles = raster.to_pmtiles(
            tif, is_cancelled=cancel, progress=self._scaled(40, 85))
        self._temps.append(pmtiles)
        self.setProgress(85)

        self._guard()
        bounds = raster.raster_bounds(tif)
        url = raster.publish(
            self._client, pmtiles, self._name, bounds=bounds,
            is_cancelled=cancel, progress=self._scaled(85, 100))
        self.setProgress(100)
        return {"name": self._name, "url": url, "pmtiles": pmtiles, "bounds": bounds}


class ActionTask(_BaseTask):
    """Run a single client call off the GUI thread (CRUD / move)."""

    def __init__(self, description, fn, on_done):
        super().__init__("geoi: " + description, on_done)
        self._fn = fn

    def work(self):
        return self._fn()


class SaveProjectTask(_BaseTask):
    def __init__(self, client, name, data, on_done, visibility=None):
        super().__init__("geoi: saving project", on_done)
        self._client = client
        self._name = name
        self._data = data
        self._visibility = visibility

    def work(self):
        return self._client.save_project(self._name, self._data, self._visibility)
