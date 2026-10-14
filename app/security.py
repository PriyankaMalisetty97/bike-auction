import hashlib, hmac, os
from datetime import timedelta
import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session
from .models import SessionLocal, User, utcnow

SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")
ALGO = "HS256"


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + h.hex()


def verify_password(pw: str, stored: str) -> bool:
    salt, h = stored.split(":")
    cand = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(cand.hex(), h)


def make_token(user: User) -> str:
    payload = {"sub": str(user.id), "role": user.role, "exp": utcnow() + timedelta(hours=12)}
    return jwt.encode(payload, SECRET, algorithm=ALGO)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    try:
        data = jwt.decode(authorization[7:], SECRET, algorithms=[ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    user = db.get(User, int(data["sub"]))
    if not user:
        raise HTTPException(401, "User not found")
    return user


def admin_only(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user
