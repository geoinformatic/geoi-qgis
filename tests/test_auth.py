import os
import sys
import threading
import urllib.parse
import urllib.request

import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geoi import auth  # noqa: E402


class SigninUrlTest(unittest.TestCase):
    def test_build_signin_url(self):
        url = auth.build_signin_url("https://geoi.de/", 54321, "ST/ATE")
        self.assertTrue(url.startswith("https://geoi.de/desktop-signin.html?"))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["port"], ["54321"])
        self.assertEqual(q["state"], ["ST/ATE"])

    def test_default_base(self):
        url = auth.build_signin_url("", 1, "x")
        self.assertTrue(url.startswith("https://geoi.de/desktop-signin.html"))


def _browser_that_returns(token, *, state_override=None):
    """A fake browser: parse the signin URL, then hit the loopback like the page."""

    def opener(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        port = q["port"][0]
        state = state_override if state_override is not None else q["state"][0]

        def hit():
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

    return opener


class WebSigninTest(unittest.TestCase):
    def test_captures_token_from_loopback(self):
        token = auth.run_web_signin(
            "https://geoi.de", open_url=_browser_that_returns("SESSION-XYZ"), timeout=10
        )
        self.assertEqual(token, "SESSION-XYZ")

    def test_rejects_wrong_state_then_times_out(self):
        # a callback whose state doesn't match must be ignored -> timeout
        with self.assertRaises(auth.AuthError):
            auth.run_web_signin(
                "https://geoi.de",
                open_url=_browser_that_returns("X", state_override="WRONG"),
                timeout=3,
            )

    def test_cancellation(self):
        with self.assertRaises(auth.AuthError):
            auth.run_web_signin(
                "https://geoi.de",
                open_url=lambda url: None,  # browser never returns
                timeout=10,
                is_cancelled=lambda: True,
            )


if __name__ == "__main__":
    unittest.main()
