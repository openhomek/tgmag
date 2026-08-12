from __future__ import annotations

ACCOUNT_PAGE_SIZE = 10
MAX_SYSTEM_ACCOUNTS = 50
ACCOUNT_CAPACITY_LOCK_ID = 84502117


def account_page_window(total: int, requested_page: int) -> tuple[int, int, int]:
    """Return the clamped page, page count, and database offset."""
    pages = (total + ACCOUNT_PAGE_SIZE - 1) // ACCOUNT_PAGE_SIZE
    page = min(requested_page, max(pages, 1))
    return page, pages, (page - 1) * ACCOUNT_PAGE_SIZE


def require_account_capacity(total: int) -> None:
    if total >= MAX_SYSTEM_ACCOUNTS:
        raise ValueError(
            f"系统账号数量已达到上限（{MAX_SYSTEM_ACCOUNTS} 个），请先移除不再使用的账号"
        )
