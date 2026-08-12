from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AccountSecurity,
    Admin,
    AuditLog,
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
from app.services.audit import audit
from app.services.pagination import ACCOUNT_CAPACITY_LOCK_ID


@dataclass(frozen=True)
class AccountDeletionResult:
    account_id: int
    phone_masked: str
    deleted_rows: int


def account_owned_delete_statements(account_id: int) -> tuple[Any, ...]:
    """Build narrowly scoped statements in foreign-key-safe deletion order."""
    return (
        delete(LoginEmailProtectionEvent).where(
            LoginEmailProtectionEvent.account_id == account_id
        ),
        delete(LoginEmailWhitelist).where(LoginEmailWhitelist.account_id == account_id),
        delete(ServiceMessage).where(ServiceMessage.account_id == account_id),
        delete(SpamCheck).where(SpamCheck.account_id == account_id),
        delete(PrivacySettings).where(PrivacySettings.account_id == account_id),
        delete(AccountSecurity).where(AccountSecurity.account_id == account_id),
        delete(TgSession).where(TgSession.account_id == account_id),
        delete(JobItem).where(JobItem.account_id == account_id),
        delete(AuditLog).where(
            AuditLog.entity_type == "account",
            AuditLog.entity_id == str(account_id),
        ),
        delete(TgAccount).where(TgAccount.id == account_id),
    )


def _without_account_reference(value: Any, account_id: int, key: str | None = None) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if child_key == "account_id" and str(child_value) == str(account_id):
                continue
            cleaned[child_key] = _without_account_reference(child_value, account_id, child_key)
        return cleaned
    if isinstance(value, list):
        values = value
        if key in {"accounts", "account_ids"}:
            values = [item for item in value if str(item) != str(account_id)]
        return [_without_account_reference(item, account_id) for item in values]
    return value


async def delete_account_records(
    session: AsyncSession,
    account_id: int,
    admin: Admin | None,
) -> AccountDeletionResult:
    """Remove one account and every active database record that identifies it."""
    await session.execute(text(f"SELECT pg_advisory_xact_lock({ACCOUNT_CAPACITY_LOCK_ID})"))
    account = await session.get(TgAccount, account_id, with_for_update=True)
    if account is None:
        raise ValueError("账号不存在或已被删除")
    phone_masked = account.phone_masked

    jobs = list(
        (
            await session.scalars(
                select(Job)
                .join(JobItem, JobItem.job_id == Job.id)
                .where(JobItem.account_id == account_id)
                .distinct()
            )
        ).all()
    )
    for job in jobs:
        original = deepcopy(job.params_json or {})
        cleaned = _without_account_reference(original, account_id)
        if cleaned != original:
            job.params_json = cleaned

    audit_rows = list((await session.scalars(select(AuditLog))).all())
    for row in audit_rows:
        if row.entity_type == "account" and row.entity_id == str(account_id):
            continue
        original = deepcopy(row.payload_json)
        cleaned = _without_account_reference(original, account_id)
        if cleaned != original:
            row.payload_json = cleaned

    deleted_rows = 0
    for statement in account_owned_delete_statements(account_id):
        result = await session.execute(statement)
        deleted_rows += max(int(result.rowcount or 0), 0)

    # Preserve only the fact that an administrator performed a deletion. This entry
    # intentionally contains no account id, phone, username, Session, or other link
    # back to the removed account.
    await audit(
        session,
        admin,
        "account_deleted",
        "system",
        payload={"removed": True},
    )
    await session.flush()
    return AccountDeletionResult(account_id, phone_masked, deleted_rows)
