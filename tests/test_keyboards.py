import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.bot.keyboards import (
    accounts_panel,
    bot_menu_button,
    login_email_account_panel,
    login_email_domains_panel,
    login_email_guard_panel,
    login_phone_panel,
    main_menu,
)
from app.config import settings
from app.main import restore_admin_keyboards


def test_main_menu_is_collapsible_and_reopenable() -> None:
    payload = main_menu().model_dump(exclude_none=True)
    assert payload["resize_keyboard"] is True
    assert payload["is_persistent"] is False
    assert payload["one_time_keyboard"] is False
    assert "remove_keyboard" not in payload


def test_project_never_sends_reply_keyboard_remove() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py"))
    assert "ReplyKeyboardRemove" not in source
    assert "remove_keyboard" not in source


def test_project_has_one_canonical_reply_keyboard_constructor() -> None:
    constructors: list[tuple[Path, int]] = []
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name == "ReplyKeyboardMarkup":
                constructors.append((path, node.lineno))

    assert len(constructors) == 1
    assert constructors[0][0] == Path("app/bot/keyboards.py")


def test_service_restart_restores_collapsible_keyboard_for_every_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "admin_ids", [101, 202])
    bot = type("BotStub", (), {"send_message": AsyncMock()})()

    asyncio.run(restore_admin_keyboards(bot))

    assert bot.send_message.await_count == 2
    assert [call.args[0] for call in bot.send_message.await_args_list] == [101, 202]
    for call in bot.send_message.await_args_list:
        payload = call.kwargs["reply_markup"].model_dump(exclude_none=True)
        assert payload["resize_keyboard"] is True
        assert payload["is_persistent"] is False
        assert payload["one_time_keyboard"] is False


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


def test_account_inline_keyboard_pages_ten_accounts_and_preserves_page() -> None:
    accounts = [
        type(
            "Account",
            (),
            {
                "id": account_id,
                "user_id": 1000 + account_id,
                "username": None,
                "phone_masked": "+44****123",
            },
        )()
        for account_id in range(11, 21)
    ]

    panel = accounts_panel(accounts, page=2, pages=4)
    account_buttons = [row[0] for row in panel.inline_keyboard[:10]]

    assert len(account_buttons) == 10
    assert account_buttons[0].callback_data == "acct:11:2"
    assert account_buttons[-1].callback_data == "acct:20:2"
    pager = panel.inline_keyboard[10]
    assert [button.callback_data for button in pager] == [
        "nav:accounts:1",
        "nav:accounts:2",
        "nav:accounts:3",
    ]


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
