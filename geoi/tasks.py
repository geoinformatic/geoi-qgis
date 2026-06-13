"""Background tasks (QgsTask) so the GUI never blocks on the network/OAuth.

Each task runs its work in ``run()`` (worker thread) and calls a Python
``on_done(ok, payload_or_error)`` callback in ``finished()`` (main thread).
"""

from qgis.core import QgsTask

from . import auth as auth_mod


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
    """Zero-config sign-in: reuse the web Google sign-in via a loopback handoff."""

    def __init__(self, client, on_done):
        super().__init__("geoi: signing in", on_done)
        self._client = client

    def work(self):
        token = auth_mod.run_web_signin(
            self._client.base_url, is_cancelled=self.isCanceled
        )
        self._client.set_token(token)
        user = self._client.me().get("user", {})
        return {"token": token, "user": user}


class CatalogTask(_BaseTask):
    """Fetch services + projects + folders in one go."""

    def __init__(self, client, on_done):
        super().__init__("geoi: loading content", on_done)
        self._client = client

    def work(self):
        groups = []
        try:
            groups = self._client.list_groups()
        except Exception:  # noqa: BLE001 - groups are optional context
            groups = []
        return {
            "services": self._client.list_services(),
            "projects": self._client.list_projects(),
            "folders": self._client.list_folders(),
            "groups": groups,
        }


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
