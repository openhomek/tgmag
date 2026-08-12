from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from app.db.models import (
    AccountSecurity,
    AuditLog,
    Base,
    Job,
    JobItem,
    LoginEmailProtectionEvent,
    LoginEmailWhitelist,
    PrivacySettings,
    ServiceMessage,
    SpamCheck,
    TgAccount,
    TgSession,
)
from app.services.account_deletion import (
    _without_account_reference,
    delete_account_records,
)
from app.tg.client_pool import ClientPool


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        phone_encrypted=f"encrypted-{account_id}",
        phone_masked=f"+44****{account_id:04d}",
        status="active",
        login_email_window_hours=0,
    )


def _seed_account_graph(session: Session, account_id: int, job_id: int) -> None:
    session.add(_account(account_id))
    session.add(TgSession(account_id=account_id, session_encrypted=f"session-{account_id}"))
    session.add(AccountSecurity(account_id=account_id, has_2fa=True))
    session.add(PrivacySettings(account_id=account_id, rules_json={"phone": "nobody"}))
    session.add(SpamCheck(account_id=account_id, status_detected="ok"))
    session.add(LoginEmailWhitelist(account_id=account_id))
    service_message = ServiceMessage(
        account_id=account_id,
        source_user_id=777000,
        message_id=account_id,
        text_hash=f"hash-{account_id}",
        text="login code",
        received_at=datetime.now(UTC),
    )
    session.add(service_message)
    session.flush()
    session.add(
        LoginEmailProtectionEvent(
            account_id=account_id,
            service_message_id=service_message.id,
            status="waiting_window",
        )
    )
    session.add(Job(id=job_id, type="send", params_json={"accounts": [account_id]}))
    session.add(JobItem(job_id=job_id, account_id=account_id, status="pending"))
    session.add(
        AuditLog(
            action="account_test",
            entity_type="account",
            entity_id=str(account_id),
            payload_json={"account_id": account_id},
        )
    )


def test_account_deletion_removes_only_the_selected_account_graph() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    models = (
        TgAccount,
        TgSession,
        AccountSecurity,
        PrivacySettings,
        SpamCheck,
        LoginEmailWhitelist,
        ServiceMessage,
        LoginEmailProtectionEvent,
        JobItem,
    )

    with Session(engine) as session:
        _seed_account_graph(session, account_id=11, job_id=101)
        _seed_account_graph(session, account_id=22, job_id=202)
        session.commit()

        class AsyncSessionAdapter:
            async def execute(self, statement):
                if isinstance(statement, TextClause):
                    return SimpleNamespace(rowcount=0)
                return session.execute(statement)

            async def scalars(self, statement):
                return session.scalars(statement)

            async def get(self, model, key, **kwargs):
                return session.get(model, key, **kwargs)

            def add(self, value):
                session.add(value)

            async def flush(self):
                session.flush()

        result = asyncio.run(delete_account_records(AsyncSessionAdapter(), 11, None))
        session.commit()

        assert result.account_id == 11
        assert result.phone_masked == "+44****0011"

        for model in models:
            target_column = model.id if model is TgAccount else model.account_id
            assert session.scalar(
                select(func.count()).select_from(model).where(target_column == 11)
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(model).where(target_column == 22)
            ) == 1
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == "11")
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == "22")
        ) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.id == 101)) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.id == 202)) == 1
        assert session.get(Job, 101).params_json == {"accounts": []}
        assert session.get(Job, 202).params_json == {"accounts": [22]}
        deletion_audits = list(
            session.scalars(select(AuditLog).where(AuditLog.action == "account_deleted"))
        )
        assert len(deletion_audits) == 1
        assert deletion_audits[0].entity_id is None
        assert deletion_audits[0].payload_json == {"removed": True}


def test_reference_scrubber_does_not_touch_other_accounts() -> None:
    payload = {
        "accounts": [11, 22, "11", "33"],
        "account_id": 11,
        "nested": {"account_id": 22, "account_ids": [11, 22]},
        "unrelated_number": 11,
    }

    assert _without_account_reference(payload, 11) == {
        "accounts": [22, "33"],
        "nested": {"account_id": 22, "account_ids": [22]},
        "unrelated_number": 11,
    }


def test_deletion_fence_cancels_only_target_account_tasks_and_connection() -> None:
    class ScalarRows:
        def all(self):
            return [91]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def scalars(self, statement):
            return ScalarRows()

    target_client = SimpleNamespace(disconnect=AsyncMock())
    other_client = SimpleNamespace(disconnect=AsyncMock())
    pool = ClientPool.__new__(ClientPool)
    pool._lock = asyncio.Lock()
    pool._deleting_account_ids = set()
    pool._service_message_locks = {11: asyncio.Lock(), 22: asyncio.Lock()}
    pool.clients = {11: target_client, 22: other_client}
    pool.sessionmaker = lambda: FakeSession()
    pool.login_email_protector = SimpleNamespace(cancel_account_tasks=AsyncMock())

    async def exercise() -> None:
        target_task = asyncio.create_task(asyncio.Event().wait(), name="login-email-window-91")
        other_task = asyncio.create_task(asyncio.Event().wait(), name="login-email-window-92")
        pool._protection_tasks = {target_task, other_task}
        await pool.begin_account_deletion(11)

        assert target_task.cancelled()
        assert not other_task.cancelled()
        assert 11 in pool._deleting_account_ids
        assert 11 not in pool.clients and 22 in pool.clients
        assert 11 not in pool._service_message_locks and 22 in pool._service_message_locks
        target_client.disconnect.assert_awaited_once()
        other_client.disconnect.assert_not_awaited()
        pool.login_email_protector.cancel_account_tasks.assert_awaited_once_with(11, {91})

        with pytest.raises(ValueError, match="正在删除"):
            await pool.get_client(11)
        await pool.end_account_deletion(11)
        other_task.cancel()
        await asyncio.gather(other_task, return_exceptions=True)

    asyncio.run(exercise())
