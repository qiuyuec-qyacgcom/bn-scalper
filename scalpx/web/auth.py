"""认证、会话与权限。

三个安全决策：
1. **口令用 PBKDF2-HMAC-SHA256 + 每用户随机盐 + 26 万轮**，不用明文/裸 sha256；
2. **会话令牌无状态**（payload + HMAC 签名），进程重启后仍有效，不需要会话表；
   代价是无法单条吊销——用 `token_version`（存在 users 表）实现"全部踢下线"。
3. **登录失败按 (IP, 用户名) 双维度限流**，防爆破又不误伤同一 NAT 下的其他人。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field

PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 16
TOKEN_TTL_S = 12 * 3600
MAX_LOGIN_FAILURES = 6
LOCKOUT_S = 300

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (hash_hex, salt_hex)。"""
    if not password or len(password) < 8:
        raise ValueError("口令长度至少 8 位")
    salt_hex = salt or os.urandom(SALT_BYTES).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return digest.hex(), salt_hex


def verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected_hex)


# --------------------------------------------------------------------------- #
# 会话令牌
# --------------------------------------------------------------------------- #
def load_or_create_secret(path: str) -> bytes:
    """服务端签名密钥。首次启动自动生成并落盘（权限 600）。"""
    p = os.path.abspath(path)
    if os.path.exists(p):
        with open(p, "rb") as fh:
            data = fh.read().strip()
            if len(data) >= 32:
                return data
    os.makedirs(os.path.dirname(p), exist_ok=True)
    secret = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=")
    with open(p, "wb") as fh:
        fh.write(secret)
    try:
        os.chmod(p, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    return secret


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_token(secret: bytes, *, uid: int, username: str, role: str, version: int, ttl_s: int = TOKEN_TTL_S) -> str:
    payload = {
        "uid": uid,
        "u": username,
        "r": role,
        "v": version,
        "exp": int(time.time()) + ttl_s,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(secret: bytes, token: str) -> dict | None:
    if not token or token.count(".") != 1:
        return None
    body, sig = token.split(".", 1)
    expect = hmac.new(secret, body.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


# --------------------------------------------------------------------------- #
# 登录限流
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class LoginLimiter:
    max_failures: int = MAX_LOGIN_FAILURES
    lockout_s: int = LOCKOUT_S
    _state: dict[str, list] = field(default_factory=dict)  # key -> [failures, locked_until]

    def _key(self, ip: str, username: str) -> str:
        return f"{ip}|{username.lower()}"

    def check(self, ip: str, username: str) -> int:
        """返回剩余锁定秒数；0 表示可以尝试。"""
        _, locked_until = self._state.get(self._key(ip, username), [0, 0.0])
        remain = int(locked_until - time.time())
        return max(0, remain)

    def record_failure(self, ip: str, username: str) -> int:
        key = self._key(ip, username)
        entry = self._state.setdefault(key, [0, 0.0])
        entry[0] += 1
        if entry[0] >= self.max_failures:
            entry[1] = time.time() + self.lockout_s
            entry[0] = 0
            return self.lockout_s
        return 0

    def reset(self, ip: str, username: str) -> None:
        self._state.pop(self._key(ip, username), None)


@dataclass(slots=True)
class Principal:
    """已认证主体。"""

    uid: int
    username: str
    role: str
    version: int = 0

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def as_dict(self) -> dict:
        return {"id": self.uid, "username": self.username, "role": self.role}


def allowed_roles(role: str, allowed: tuple[str, ...]) -> bool:
    return role in allowed
