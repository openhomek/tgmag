from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.login_email_protection import parse_login_email_window_hours
from app.webapp.server import qr_login_payload, reusable_phone_login


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, 0), (24, 24), ("8", 8), (720, 720)],
)
def test_parse_login_email_window_hours(raw: object, expected: int) -> None:
    assert parse_login_email_window_hours(raw) == expected


@pytest.mark.parametrize("raw", [None, True, -1, 721, 1.5, "abc"])
def test_parse_login_email_window_hours_rejects_invalid_values(raw: object) -> None:
    with pytest.raises(ValueError, match="0–720"):
        parse_login_email_window_hours(raw)


def test_qr_login_payload_contains_local_png_and_expiry() -> None:
    expires = datetime.now(UTC) + timedelta(seconds=30)
    payload = qr_login_payload(
        SimpleNamespace(url="tg://login?token=test-token", expires=expires)
    )

    prefix = "data:image/png;base64,"
    assert payload["qr_image"].startswith(prefix)
    assert base64.b64decode(payload["qr_image"][len(prefix) :]).startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    assert payload["expires_at"] == expires.isoformat()


def test_recent_phone_login_is_reused_without_requesting_another_code() -> None:
    app = {
        "pending_logins": {
            "7:recent": {
                "method": "phone",
                "phone": "+447700900123",
                "created_at": time.time() - 10,
                "delivery": {"timeout": 60, "label": "Telegram App 内验证码"},
            },
            "7:expired": {
                "method": "phone",
                "phone": "+441234567890",
                "created_at": time.time() - 120,
                "delivery": {"timeout": 60},
            },
        }
    }

    reusable = reusable_phone_login(app, 7, "+447700900123")
    assert reusable is not None and reusable[0] == "recent"
    assert reusable_phone_login(app, 7, "+441234567890") is None
    assert reusable_phone_login(app, 8, "+447700900123") is None


def test_mini_app_actions_enable_compact_view_specific_layout() -> None:
    index = Path("app/webapp/static/index.html").read_text(encoding="utf-8")
    script = Path("app/webapp/static/app.js").read_text(encoding="utf-8")
    styles = Path("app/webapp/static/styles.css").read_text(encoding="utf-8")

    assert '<body data-view="dashboard">' in index
    assert "20260812-ui4" in index
    assert "document.body.dataset.view = view" in script
    assert "tg.disableVerticalSwipes?.()" in script
    assert 'height: var(--tg-viewport-stable-height, 100dvh)' in styles
    assert "overflow-y: auto" in styles
    assert 'const shell = qs(".shell")' in script
    assert 'body[data-view="actions"] .action-pane .panel' in styles
