import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.bot.handlers import login_email_edit_address, login_email_edit_code
from app.bot.keyboards import account_actions_panel
from app.services.login_email_protection import LoginEmailProtector


def test_account_has_separate_login_email_configuration():
    buttons = [b for row in account_actions_panel(42).inline_keyboard for b in row]
    assert any(b.text == "配置登录邮箱" and b.callback_data == "loginemail:configure:42" for b in buttons)
    assert any(b.callback_data == "emailguard:account:42" for b in buttons)


def test_invalid_email_does_not_request_code():
    message = SimpleNamespace(text="invalid", answer=AsyncMock())
    pool = SimpleNamespace(get_client=AsyncMock())
    asyncio.run(login_email_edit_address(message, AsyncMock(), Mock(), pool))
    pool.get_client.assert_not_awaited()


def test_manual_email_wait_blocks_other_changes():
    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector._change_locks = {}
    protector.manual_email_waits = {42: datetime.now(timezone.utc) + timedelta(minutes=5)}
    assert protector.has_change_in_progress(42)
    assert not protector.has_change_in_progress(43)
    protector.manual_email_waits[42] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not protector.has_change_in_progress(42)


def test_email_confirmation_saves_only_confirmed_address(monkeypatch):
    from app.bot import handlers
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    state = AsyncMock()
    state.get_data.return_value = dict(account_id=42, deadline=deadline.isoformat(), email="me@example.com", code_length=6)
    security = SimpleNamespace(login_email_encrypted=None)
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.get.return_value = security
    protector = LoginEmailProtector.__new__(LoginEmailProtector)
    protector._change_locks = {}
    protector.manual_email_waits = {42: deadline}
    pool = SimpleNamespace(get_client=AsyncMock(), login_email_protector=protector)
    message = SimpleNamespace(text="123456", from_user=SimpleNamespace(id=7), answer=AsyncMock())
    confirm = AsyncMock(return_value=SimpleNamespace(email="me@example.com"))
    monkeypatch.setattr(handlers.account_ops, "confirm_login_email", confirm)
    monkeypatch.setattr(handlers, "encrypt_text", lambda text: "encrypted:" + text)
    monkeypatch.setattr(handlers, "audit", AsyncMock())
    asyncio.run(login_email_edit_code(message, state, lambda: session, pool))
    assert security.login_email_encrypted == "encrypted:me@example.com"
    session.commit.assert_awaited_once()
    state.clear.assert_awaited_once()
    assert 42 not in protector.manual_email_waits


def test_expired_code_does_not_call_telegram():
    state = AsyncMock()
    state.get_data.return_value = dict(account_id=42, deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    pool = SimpleNamespace(get_client=AsyncMock())
    message = SimpleNamespace(answer=AsyncMock())
    asyncio.run(login_email_edit_code(message, state, Mock(), pool))
    pool.get_client.assert_not_awaited()
    state.clear.assert_awaited_once()
