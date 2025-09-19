"""Simple bearer token authentication dependency for FastAPI routes."""
from fastapi import Header, HTTPException, status


def bearer_auth(auth_token: str):
    """Return a dependency that enforces a static bearer token.

    Passing an empty token disables authentication entirely, allowing local
    development environments to opt out without modifying route definitions.
    """

    if not auth_token:
        async def _noop_auth(authorization: str = Header(default=None)) -> None:  # noqa: ARG001
            return None

        return _noop_auth

    async def _auth(authorization: str = Header(default=None)) -> None:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing bearer token",
            )
        token = authorization.split(" ", 1)[1].strip()
        if token != auth_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid token",
            )

    return _auth
