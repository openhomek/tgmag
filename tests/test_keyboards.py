from pathlib import Path

import pytest

from app.bot.keyboards import (
    bot_menu_button,
    login_email_account_panel,
    login_email_domains_panel,
    login_email_guard_panel,
    login_phone_panel,
    main_menu,
)
from app.config import settings


def test_main_menu_is_collapsible_and_reopenable() -> None:
    payload = main_menu().model_dump(exclude_none=True)
    assert payload["resize_keyboard"] is True
    assert payload["is_persistent"] is False
    assert payload["one_time_keyboard"] is False
    assert "remove_keyboard" not in payload


def test_project_never_sends_reply_keyboard_remove() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py"))
    assert "ReplyKeyboardRemove" not in source
    assert "remove_keyboard=True" not in source
    assert '"remove_keyboard": true' not in source.lower()


def test_main_navigation_labels_are_reply_keyboard_buttons() -> None:
    labels = {button.text for row in main_menu().keyboard for button in row}
    assert labels == {
        "系统状态",
        "账号管理",
        "登录账号",
        "扫码登录",
        "安全防护",
    }
    assert len(main_menu().keyboard) == 3


def test_advanced_features_stay_out_of_reply_keyboard() -> None:
    labels = {button.text for row in main_menu().keyboard for button in row}
    assert labels.isdisjoint(
        {
            "导入Session",
            "导出Session",
            "批量导入Session",
            "批量任务",
            "目标与速率",
            "监控中心",
            "内置应用",
        }
    )


def test_phone_login_offers_qr_fallback() -> None:
    callbacks = {
        button.callback_data for row in login_phone_panel().inline_keyboard for button in row
    }
    assert callbacks == {"login:qr", "flow:cancel"}


def test_native_bot_menu_opens_configured_mini_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mini_app_enabled", True)
    monkeypatch.setattr(settings, "mini_app_public_url", "https://bot.example/mini-app")
    payload = bot_menu_button().model_dump(exclude_none=True)
    assert payload["type"] == "web_app"
    assert payload["text"] == "打开"
    assert payload["web_app"]["url"].endswith("/mini-app")


def test_security_center_separates_domains_whitelist_events_and_gmail() -> None:
    callbacks = {
        button.callback_data for row in login_email_guard_panel().inline_keyboard for button in row
    }
    assert {
        "emailguard:domains",
        "emailguard:whitelist",
        "emailguard:events",
        "emailguard:testimap",
        "emailguard:checkall",
    } <= callbacks


def test_domain_deletion_requires_confirmation() -> None:
    panel = login_email_domains_panel(("one.example", "two.example"), "one.example")
    callbacks = [button.callback_data for row in panel.inline_keyboard for button in row]
    assert "emailguard:deleteask:0" in callbacks
    assert "emailguard:delete:0" not in callbacks


def test_account_email_guard_has_window_setting_button() -> None:
    callbacks = {
        button.callback_data
        for row in login_email_account_panel(15, False).inline_keyboard
        for button in row
    }
    assert "emailguard:window:15" in callbacks
