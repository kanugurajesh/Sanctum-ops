from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def issue_token(user_id: int, hospital_id: int) -> str:
    s = get_settings()
    now = datetime.now(tz=timezone.utc)
    return jwt.encode(
        {"sub": str(user_id), "hid": hospital_id, "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes)},
        s.jwt_secret, algorithm="HS256",
    )


def decode_token(token: str) -> tuple[int, int]:
    data = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    return int(data["sub"]), int(data["hid"])
