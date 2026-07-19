import json
import secrets
import time

from config import DATA_DIR

_SESSIONS_FILE = DATA_DIR / "sessions.json"
_SESSIONS: dict[str, float] = {}
SESSION_TTL = 86400 * 7  # 7 days


def _load() -> None:
    if not _SESSIONS_FILE.exists():
        return
    try:
        now  = time.time()
        data = json.loads(_SESSIONS_FILE.read_text())
        _SESSIONS.update({k: v for k, v in data.items() if isinstance(v, (int, float)) and v > now})
    except Exception:
        pass


def _save() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SESSIONS_FILE.write_text(json.dumps(_SESSIONS))


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + SESSION_TTL
    _save()
    return token


def verify_session(token: str) -> bool:
    exp = _SESSIONS.get(token)
    if exp is None:
        return False
    if time.time() > exp:
        del _SESSIONS[token]
        _save()
        return False
    return True


def revoke_session(token: str) -> None:
    _SESSIONS.pop(token, None)
    _save()


_load()
