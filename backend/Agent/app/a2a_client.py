"""A2A client: discover the Google ADK agents by their agent cards and call
them over the Agent-to-Agent protocol (v1.0, JSON-RPC binding).

  discovery   GET  {ADK_AGENTS_URL}/{name}/.well-known/agent-card.json
  invocation  POST <url from the card's supportedInterfaces>  method SendMessage

llm.complete() falls back to an in-process model call when the remote agent
is unavailable, so a down adk-agents container slows the audit but never
blocks it.
"""
import logging
import os
import uuid

import httpx

log = logging.getLogger(__name__)

AGENTS = ("translation", "reporting")
_cards: dict[str, dict] = {}


class A2AError(RuntimeError):
    pass


def base_url() -> str:
    return os.getenv("ADK_AGENTS_URL", "").rstrip("/")


def configured() -> bool:
    """Without ADK_AGENTS_URL every call runs in-process (tests, or no adk-agents container)."""
    return bool(base_url())


def discover(name: str, *, refresh: bool = False) -> dict:
    """Fetch (and cache) the agent card that advertises the agent's skills and endpoint."""
    if name not in _cards or refresh:
        try:
            r = httpx.get(f"{base_url()}/{name}/.well-known/agent-card.json", timeout=5)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise A2AError(f"A2A agent '{name}' is not reachable: {exc}") from exc
        _cards[name] = r.json()
    return _cards[name]


def _endpoint(card: dict) -> str:
    for iface in card.get("supportedInterfaces") or []:
        if iface.get("protocolBinding") == "JSONRPC" and str(iface.get("url") or "").startswith(base_url() + "/"):
            return iface["url"]  # only on the configured host, so a card can't redirect invoice text
    raise A2AError(f"Agent card for {card.get('name')} has no JSON-RPC interface")


def send(name: str, text: str, *, timeout: float = 120) -> str:
    """Send one text message to an ADK agent and return its text answer."""
    card = discover(name)
    payload = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendMessage",
               "params": {"message": {"role": "ROLE_USER", "messageId": uuid.uuid4().hex,
                                      "parts": [{"text": text}]}}}
    try:
        r = httpx.post(_endpoint(card), json=payload, headers={"A2A-Version": "1.0"}, timeout=timeout)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        _cards.pop(name, None)  # re-discover next time (the container may have restarted)
        raise A2AError(f"A2A call to {name} failed: {exc}") from exc
    if "error" in body:
        raise A2AError(f"A2A {name} returned error: {body['error']}")
    result = body.get("result") or {}
    task, message = result.get("task"), result.get("message")
    if task:
        state = (task.get("status") or {}).get("state")
        if state != "TASK_STATE_COMPLETED":
            raise A2AError(f"A2A {name} task ended in state {state}")
        parts = [p for a in task.get("artifacts") or [] for p in a.get("parts") or []]
    else:
        parts = (message or {}).get("parts") or []
    answer = "".join(p.get("text", "") for p in parts).strip()
    if not answer:
        raise A2AError(f"A2A {name} returned no text")
    return answer


def cards() -> dict:
    """Agent cards for the dashboard; unreachable agents are reported, not raised."""
    out = {}
    for name in AGENTS:
        try:
            out[name] = discover(name, refresh=True)
        except A2AError as exc:
            out[name] = {"error": str(exc)}
    return out
