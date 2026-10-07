"""Sending through Gmail with OAuth, for an account that cannot have an App Password."""
from __future__ import annotations

import base64

from app.config import Settings
from app.services import notifier


def _settings(**over):
    base = dict(SMTP_USERNAME="galaxy@a3brands.com", SMTP_PASSWORD="", NOTIFY_TO="a@b.test",
                GMAIL_OAUTH_CLIENT_ID="cid", GMAIL_OAUTH_CLIENT_SECRET="sec",
                GMAIL_OAUTH_REFRESH_TOKEN="refresh")
    base.update(over)
    return Settings(**base)


def test_an_oauth_token_stands_in_for_the_password():
    assert _settings().notify_config_problem() is None
    assert "SMTP_PASSWORD" in (_settings(GMAIL_OAUTH_REFRESH_TOKEN="").notify_config_problem() or "")


def test_xoauth2_login_sends_the_bearer_token(monkeypatch):
    monkeypatch.setattr(notifier, "_oauth_access_token", lambda s: "ya29.token")
    seen = {}

    class Server:
        def docmd(self, cmd, arg):
            seen["cmd"], seen["arg"] = cmd, arg
            return 235, b"Accepted"

    notifier._oauth_login(Server(), _settings())
    assert seen["cmd"] == "AUTH" and seen["arg"].startswith("XOAUTH2 ")
    raw = base64.b64decode(seen["arg"].split(" ", 1)[1]).decode()
    assert raw == "user=galaxy@a3brands.com\x01auth=Bearer ya29.token\x01\x01"
