from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status


async def require_admin_token(
    request: Request,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    configured = request.app.state.services.settings.admin_action_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="manual admin actions are not configured",
        )
    if admin_token is None or not hmac.compare_digest(admin_token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid admin action token",
        )
