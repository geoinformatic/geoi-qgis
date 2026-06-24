"""Tests for the QGIS-plugin sign-in regressions + raster publish progress/cancel.

Covers three fixes and one feature, all off real QGIS/GDAL:

  BUG 1  the loopback sign-in server stays bound+listening for the WHOLE
         window even when a NON-genuine cancel is polled, so a slow OAuth
         redirect is captured rather than refused (ERR_CONNECTION_REFUSED).
  BUG 3  clicking "Sign in" does NOT block the UI thread on /auth/providers —
         the availability check runs INSIDE the background task.
  FEATURE  the raster publish task reports progress, aborts on cancel, cleans
           up temp files, and NEVER confirms a half-upload.
"""

import os
import sys
import threading
import time
import types
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import auth  # noqa: E402


# ----------------------------------------------------------- BUG 1: loopback
def _delayed_browser(token, *, delay, started=None):
    """A fake browser that hits the loopback callback only AFTER ``delay`` s.

    Mirrors a real OAuth round-trip: the redirect lands well after the browser
    is "opened". ``started`` (an Event) is set the moment the opener returns,
    so a test can begin polling a spurious cancel while the redirect is still
    pending.
    """

    def opener(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        port = q["port"][0]
        state = q["state"][0]

        def hit():
            time.sleep(delay)
            cb = "http://127.0.0.1:{}/?token={}&state={}".format(
                port,
                urllib.parse.quote(token),
                urllib.parse.quote(state),
            )
            try:
                urllib.request.urlopen(cb, timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=hit, daemon=True).start()
        if started is not None:
            started.set()

    return opener


class LoopbackResilientToSpuriousCancelTest(unittest.TestCase):
    """The decisive BUG 1 contract: a TRANSIENT/spurious ``is_cancelled`` (a
    QgsTask teardown flicker) must NOT close the listening socket before the
    redirect arrives. The server stays up; the token is captured."""

    def test_token_captured_despite_a_spurious_cancel_flicker(self):
        # Cancel returns True exactly ONCE then False forever — a flicker, not
        # a sustained user cancel. The redirect arrives ~0.6 s later, well
        # within the window. The token MUST still be captured.
        calls = {"n": 0}

        def flicker():
            calls["n"] += 1
            return calls["n"] == 1  # True once, then False

        token = auth.run_web_signin(
            "https://geoi.de",
            open_url=_delayed_browser("SESSION-LATE", delay=0.6),
            timeout=10,
            is_cancelled=flicker,
        )
        self.assertEqual(token, "SESSION-LATE")

    def test_token_captured_when_browser_is_slow_to_open(self):
        # Even with a slow browser (the redirect lands ~0.8 s after open) and
        # NO cancel, the socket is listening the whole time — never refused.
        token = auth.run_web_signin(
            "https://geoi.de",
            open_url=_delayed_browser("SESSION-SLOW", delay=0.8),
            timeout=10,
        )
        self.assertEqual(token, "SESSION-SLOW")

    def test_genuine_sustained_cancel_still_aborts(self):
        # A cancel that PERSISTS is a real user cancel and must abort.
        with self.assertRaises(auth.AuthError):
            auth.run_web_signin(
                "https://geoi.de",
                open_url=lambda url: None,  # browser never returns
                timeout=10,
                is_cancelled=lambda: True,  # sustained
            )


# ------------------------------------------------------- tasks import helper
def _import_tasks(is_cancelled_seq=None, progress_sink=None):
    """Import ``geoi.tasks`` with a minimal ``qgis.core`` QgsTask stub.

    ``is_cancelled_seq`` — a callable returning the next ``isCanceled()`` value;
    ``progress_sink`` — a list that records every ``setProgress`` value.
    """
    saved = {k: sys.modules.get(k) for k in ("qgis", "qgis.core")}
    qgis = types.ModuleType("qgis")
    qgis_core = types.ModuleType("qgis.core")

    sink = progress_sink if progress_sink is not None else []
    cancel = is_cancelled_seq or (lambda: False)

    class _QgsTask:
        def __init__(self, *a, **k):
            pass

        def setProgress(self, value):
            sink.append(value)

        def isCanceled(self):
            return cancel()

    qgis_core.QgsTask = _QgsTask
    qgis.core = qgis_core
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = qgis_core

    import geoi as geoi_pkg
    saved_task_attr = getattr(geoi_pkg, "tasks", None)
    # Force a genuinely fresh tasks module bound to THIS QgsTask stub: clear
    # both the sys.modules entry AND the package attribute (``from geoi import
    # tasks`` resolves the cached attribute first, otherwise a stale module
    # from a previous import leaks in).
    sys.modules.pop("geoi.tasks", None)
    if hasattr(geoi_pkg, "tasks"):
        delattr(geoi_pkg, "tasks")
    import importlib
    tasks = importlib.import_module("geoi.tasks")

    def cleanup():
        sys.modules.pop("geoi.tasks", None)
        if saved_task_attr is not None:
            geoi_pkg.tasks = saved_task_attr
        elif hasattr(geoi_pkg, "tasks"):
            delattr(geoi_pkg, "tasks")
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return tasks, sink, cleanup


# -------------------------------------------------- BUG 3: sign-in is prompt
class SignInDoesNotBlockOnProvidersTest(unittest.TestCase):
    """Clicking "Sign in" must NOT call ``auth_providers`` on the UI thread
    before opening the browser. Structural guards on the source + behavioural
    proof that SignInTask.work does the provider check itself, fail-OPEN."""

    def _read(self, name):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "geoi", name)
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_plugin_sign_in_does_not_call_auth_providers(self):
        # The whole BUG 3 fix: the synchronous main-thread provider call is
        # gone from sign_in(); the browser opens immediately.
        src = self._read("plugin.py")
        start = src.index("def sign_in(self):")
        nxt = src.index("\n    def ", start + 1)
        body = src[start:nxt]
        self.assertNotIn("auth_providers", body,
                         "sign_in must not block the UI on /auth/providers")

    def test_tasks_provider_check_uses_a_short_timeout_and_fails_open(self):
        src = self._read("tasks.py")
        # The provider availability check moved into SignInTask with a SHORT,
        # bounded timeout (so a slow endpoint can't freeze the click).
        self.assertIn("PROVIDER_TIMEOUT", src)
        self.assertIn("auth_providers(timeout=", src)

    def test_signin_task_opens_browser_when_providers_slow_or_error(self):
        # FAIL OPEN: a provider lookup that errors/times out must NOT block the
        # loopback flow — work() proceeds to run_web_signin.
        tasks, _sink, cleanup = _import_tasks()
        try:
            opened = {"n": 0}

            class _Client:
                base_url = "https://geoi.de"

                def auth_providers(self, timeout=None):
                    raise RuntimeError("slow / unreachable")

                def set_token(self, t):
                    self._t = t

                def me(self):
                    return {"user": {"email": "x@y.z"}}

            # Stub the loopback so the test stays a pure unit.
            orig = tasks.auth_mod.run_web_signin
            tasks.auth_mod.run_web_signin = (
                lambda base, is_cancelled=None: (opened.__setitem__("n", 1)
                                                 or "TOK"))
            try:
                task = tasks.SignInTask(_Client(), lambda ok, p: None)
                res = task.work()
            finally:
                tasks.auth_mod.run_web_signin = orig

            self.assertEqual(opened["n"], 1, "browser flow must run despite a "
                             "slow/erroring provider lookup")
            self.assertEqual(res["token"], "TOK")
        finally:
            cleanup()

    def test_signin_task_reports_disabled_when_providers_empty(self):
        # A DEFINITIVE empty list short-circuits with the SIGNIN_DISABLED
        # verdict and never opens a browser.
        tasks, _sink, cleanup = _import_tasks()
        try:
            opened = {"n": 0}

            class _Client:
                base_url = "https://geoi.de"

                def auth_providers(self, timeout=None):
                    return []

            orig = tasks.auth_mod.run_web_signin
            tasks.auth_mod.run_web_signin = (
                lambda base, is_cancelled=None: (opened.__setitem__("n", 1)
                                                 or "TOK"))
            try:
                task = tasks.SignInTask(_Client(), lambda ok, p: None)
                ok = task.run()
            finally:
                tasks.auth_mod.run_web_signin = orig

            self.assertFalse(ok)
            self.assertEqual(opened["n"], 0, "no browser when sign-in disabled")
            self.assertTrue(task._error.startswith(tasks.SIGNIN_DISABLED))
        finally:
            cleanup()


# --------------------------------------------- FEATURE: raster progress/cancel
class _FakeRasterModule:
    """A drop-in ``geoi.raster`` stub that records progress + cancel and lets a
    test flip cancel mid-pipeline. Each stage creates a REAL temp file so the
    cancel-cleanup is genuinely verified on disk."""

    def __init__(self, tmpdir, cancel_after_stage=None):
        self._dir = tmpdir
        self._cancel_after = cancel_after_stage
        self.stage = 0
        self.made = []
        self.confirmed = False  # set True only if publish() reaches confirm

        # Re-export the real exception types so the task's except clauses match.
        from geoi import raster as real
        self.RasterError = real.RasterError
        self.RasterCancelled = real.RasterCancelled
        self.cleanup_temp = real.cleanup_temp

    def _touch(self, suffix):
        path = os.path.join(self._dir, "out" + suffix)
        with open(path, "wb") as fh:
            fh.write(b"x" * 16)
        self.made.append(path)
        return path

    def _advance(self, is_cancelled, progress, lo_hi=None):
        self.stage += 1
        if progress:
            progress(50)  # some mid-stage progress
        # honour cancel exactly like the real stages do
        if is_cancelled and is_cancelled():
            raise self.RasterCancelled("Publishing cancelled.")

    def build_vrt(self, inputs, is_cancelled=None):
        self._advance(is_cancelled, None)
        return self._touch(".vrt")

    def warp_to_3857(self, vrt, is_cancelled=None, progress=None):
        self._advance(is_cancelled, progress)
        return self._touch(".3857.tif")

    def to_pmtiles(self, tif, is_cancelled=None, progress=None):
        self._advance(is_cancelled, progress)
        return self._touch(".pmtiles")

    def raster_bounds(self, tif):
        return [0, 0, 1, 1]

    def publish(self, client, pmtiles, name, bounds=None, is_cancelled=None,
                progress=None):
        if is_cancelled and is_cancelled():
            raise self.RasterCancelled("Publishing cancelled.")
        self.confirmed = True
        if progress:
            progress(100)
        return "https://cdn/x.pmtiles"


class RasterPublishProgressCancelTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_task(self, cancel_at_stage):
        """Run RasterPublishTask with a fake raster module that flips cancel
        true once ``cancel_at_stage`` stages have run. Returns (ok, task, fake,
        progress_sink)."""
        # Flip cancel true after N stages have started.
        state = {"on": False}
        progress = []
        tasks, sink, cleanup = _import_tasks(
            is_cancelled_seq=lambda: state["on"], progress_sink=progress)
        try:
            import geoi as geoi_pkg
            fake = _FakeRasterModule(self._tmp)
            saved = sys.modules.get("geoi.raster")
            saved_attr = getattr(geoi_pkg, "raster", None)
            sys.modules["geoi.raster"] = fake
            # `from . import raster` resolves via the package attribute first,
            # so point that at the fake too.
            geoi_pkg.raster = fake

            # Arm cancel: monkeypatch the fake's _advance to flip the flag once
            # the requested number of stages have run.
            orig_advance = fake._advance

            def advance(is_cancelled, prog, lo_hi=None):
                orig_advance(is_cancelled, prog, lo_hi)
                if fake.stage >= cancel_at_stage:
                    state["on"] = True

            fake._advance = advance

            try:
                task = tasks.RasterPublishTask(
                    object(), ["/in.tif"], "tiles", lambda ok, p: None)
                ok = task.run()
            finally:
                if saved is not None:
                    sys.modules["geoi.raster"] = saved
                else:
                    sys.modules.pop("geoi.raster", None)
                if saved_attr is not None:
                    geoi_pkg.raster = saved_attr
            return ok, task, fake, sink
        finally:
            cleanup()

    def test_progress_advances_through_the_stages(self):
        # No cancel: progress is reported and ends at 100.
        ok, task, fake, sink = self._run_task(cancel_at_stage=99)
        self.assertTrue(ok)
        self.assertTrue(sink, "the task must report progress")
        self.assertEqual(sink[-1], 100)
        # Monotonic non-decreasing — a real progress bar never goes backwards.
        self.assertEqual(sink, sorted(sink))
        self.assertTrue(fake.confirmed, "a successful publish confirms")

    def test_cancel_mid_pipeline_aborts_and_cleans_up_and_never_confirms(self):
        # Cancel flips true after the warp stage (stage 2). The pipeline must
        # abort, delete every temp file it made, and NEVER confirm the upload.
        ok, task, fake, sink = self._run_task(cancel_at_stage=2)
        self.assertFalse(ok)
        self.assertEqual(task._error, "Publishing cancelled.")
        self.assertFalse(fake.confirmed,
                         "a cancelled publish must never confirm a half-upload")
        # Every temp file created by the (partial) pipeline is gone.
        for path in fake.made:
            self.assertFalse(os.path.exists(path),
                             "temp file not cleaned up: " + path)


class ToPmtilesMbtilesCleanupTest(unittest.TestCase):
    """The intermediate WebP MBTiles that ``to_pmtiles`` writes BEFORE the
    PMTiles conversion must be removed even when a cancel fires between the
    GDAL Translate and step 3 — otherwise a cancelled publish leaks a large
    temp. Exercised against the REAL ``geoi.raster.to_pmtiles`` with a fake
    GDAL so no WebP/GDAL build is needed."""

    def test_mbtiles_removed_on_cancel_after_translate(self):
        import tempfile
        import shutil
        from geoi import raster as R

        tmp = tempfile.mkdtemp()
        try:
            tif = os.path.join(tmp, "x.3857.tif")
            with open(tif, "wb") as fh:
                fh.write(b"\0")
            mbtiles = os.path.join(tmp, "x.mbtiles")
            state = {"translated": False}

            class _FakeDS:
                def BuildOverviews(self, *a, **k):
                    pass

                def FlushCache(self):
                    pass

            class _FakeGdal:
                def Translate(self, dst, src, **kw):
                    # A real, non-trivial MBTiles intermediate on disk.
                    with open(dst, "wb") as fh:
                        fh.write(b"\0" * 4096)
                    state["translated"] = True
                    return _FakeDS()

            # Cancel reads False until Translate has run, then True — so the
            # _check_cancel that follows Translate raises mid-pipeline.
            def cancel():
                return state["translated"]

            orig = (R._gdal, R.assert_target_crs, R._pmtiles_writer)
            R._gdal = lambda: _FakeGdal()
            R.assert_target_crs = lambda p: None
            R._pmtiles_writer = lambda: ("python", "vendored")
            try:
                with self.assertRaises(R.RasterCancelled):
                    R.to_pmtiles(tif, is_cancelled=cancel)
            finally:
                R._gdal, R.assert_target_crs, R._pmtiles_writer = orig

            self.assertTrue(state["translated"], "the fake Translate must run")
            self.assertFalse(
                os.path.exists(mbtiles),
                "the intermediate .mbtiles must be removed on a mid-pipeline cancel",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
