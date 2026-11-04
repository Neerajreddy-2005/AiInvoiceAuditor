"""Redis cache. Disposable by design.

Only recomputable values live here (LLM responses, translations,
embeddings, RAG answers), each with an expiry. If Redis is flushed or
briefly unavailable, lookups miss and the system only gets slower.
"""
import hashlib
import json
import logging

import redis

log = logging.getLogger(__name__)
_client: redis.Redis | None = None


def init(url: str) -> None:
    global _client
    _client = redis.Redis.from_url(url, decode_responses=True,
                                   socket_timeout=2, socket_connect_timeout=2)
    _client.ping()


def ping() -> None:
    _client.ping()


def make_key(prefix: str, *parts) -> str:
    """e.g. llm:{sha256(model + prompt)}"""
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def get_json(key: str):
    if _client is None:
        return None
    try:
        value = _client.get(key)
    except redis.RedisError as exc:
        log.warning("Cache read failed (%s); continuing without cache", exc)
        return None
    try:
        return json.loads(value) if value else None
    except ValueError:
        log.warning("Ignoring unreadable cache entry")
        return None


def set_json(key: str, value, ttl_seconds: int) -> None:
    if _client is None:
        return
    try:
        _client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl_seconds)
    except redis.RedisError as exc:
        log.warning("Cache write failed (%s); continuing without cache", exc)


def delete(key: str) -> None:
    if _client is None:
        return
    try:
        _client.delete(key)
    except redis.RedisError:
        pass
