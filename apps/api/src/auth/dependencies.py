"""
Auth Dependencies

FastAPI dependencies for authentication.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """
    Get current authenticated user.

    TODO: Implement actual JWT validation
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # TODO: Validate JWT and get user from database
    # For now, return a placeholder
    return {
        "id": "placeholder-user-id",
        "email": "user@example.com",
        "name": "Test User",
        "token": credentials.credentials,
    }


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    """
    Get current user if authenticated, None otherwise.

    Useful for endpoints that work for both authenticated and anonymous users.
    """
    if credentials is None:
        return None

    # TODO: Validate JWT and get user from database
    return {
        "id": "placeholder-user-id",
        "email": "user@example.com",
        "name": "Test User",
        "token": credentials.credentials,
    }
