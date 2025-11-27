"""Centralized environment/config helpers."""

import os


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def crcon_url(default: str = "http://localhost:8010") -> str:
    return os.getenv("CRCON_URL", default).strip()


def crcon_api_key() -> str:
    return (os.getenv("CRCON_API_KEY") or "").strip()


def enable_kill_feed(default: bool = False) -> bool:
    return _get_bool("ENABLE_KILL_FEED", default)


def crcon_timeout(default: int = 15) -> int:
    return _get_int("CRCON_TIMEOUT", default)


def tanks_file(default: str = "tanks.json") -> str:
    return os.getenv("TANKS_FILE", default).strip()


def kill_webhook_port(default: int = 8081) -> int:
    raw = os.getenv("KILL_WEBHOOK_PORT")
    fallback_port = os.getenv("PORT")

    def _parse(val: str) -> int | None:
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    # Handle common placeholder usage KILL_WEBHOOK_PORT=$PORT
    if raw:
        if raw.strip() == "$PORT" and fallback_port:
            parsed = _parse(fallback_port)
            if parsed:
                return parsed
        parsed = _parse(raw)
        if parsed:
            return parsed

    if fallback_port:
        parsed = _parse(fallback_port)
        if parsed:
            return parsed

    return default


def kill_webhook_host(default: str = "0.0.0.0") -> str:
    return os.getenv("KILL_WEBHOOK_HOST", default).strip()


def kill_webhook_path(default: str = "/kill-webhook") -> str:
    value = os.getenv("KILL_WEBHOOK_PATH", default).strip()
    return value if value.startswith("/") else f"/{value}"


def kill_webhook_secret(default: str = "") -> str:
    return os.getenv("KILL_WEBHOOK_SECRET", default).strip()
