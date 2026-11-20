"""The only way the API learns who is acting: a signed session token.
Roles and permissions are then loaded fresh from the database on every request."""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.security import decode_token
from app.db.engine import tenant_tx
from app.execution import directory
from app.policy.principal import Principal

bearer = HTTPBearer(auto_error=False)


def get_principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    try:
        user_id, hospital_id = decode_token(creds.credentials)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    with tenant_tx(hospital_id) as conn:
        catalog = directory.load_catalog(conn)
        p = directory.load_principal(conn, user_id, hospital_id, catalog)
    if p is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    return p
