"""LLM gateway: LiteLLM (in-process) to the Gemini API, with a Redis cache.

Every chat call is cached under llm:{sha256(model + messages)}, so a
repeated prompt is answered from Redis instead of Gemini.

Each task can use its own model (llm.task_models in agent.yaml); a task
model that fails falls back to chat_model. A call can also be handed to a
Google ADK agent over A2A (agent=...): it still goes through the cache and
the daily limit here, and runs in-process if that agent is unavailable.
"""
import json
import logging
import math
import os
import re

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no network fetch at import

import litellm  # noqa: E402

from . import a2a_client, cache, tracing  # noqa: E402
from .settings import agent_config, prompts  # noqa: E402

log = logging.getLogger(__name__)
litellm.drop_params = True
litellm.suppress_debug_info = True


class LLMError(RuntimeError):
    pass


class DailyLimitReached(LLMError):
    pass


# Task models the account may not use; skipped until the process restarts.
_denied_models: set[str] = set()
_DENIED = ("permission", "not authorized", "accessdenied", "not found")


def model_for(task: str | None) -> str:
    llm_cfg = agent_config()["llm"]
    model = (llm_cfg.get("task_models") or {}).get(task) or llm_cfg["chat_model"]
    return llm_cfg["chat_model"] if model in _denied_models else model


def render(template: str, **values) -> str:
    """One pass over the template: a {{placeholder}} inside invoice text is never expanded."""
    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        return "" if values[name] is None else str(values[name])
    return re.sub(r"\{\{(\w+)\}\}", fill, template)


def unit_score(value) -> float:
    """A model-reported score as 0..1. Anything else, including NaN, counts as 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number)) if math.isfinite(number) else 0.0


def _safe_error(exc: Exception) -> str:
    """Provider errors can include the request URL, and Gemini's URLs carry the API key."""
    text = re.sub(r"([?&]key=)[^&\s\"']+", r"\1[hidden]", str(exc))
    key = os.getenv("GEMINI_API_KEY")
    return text.replace(key, "[hidden]") if key else text


def prompt_messages(name: str, **values) -> list[dict]:
    """Build [system persona, user prompt] from configs/prompts.yaml."""
    cfg = prompts()
    spec = cfg["prompts"][name]
    persona = cfg["personas"][spec["persona"]]
    return [{"role": "system", "content": persona.strip()},
            {"role": "user", "content": render(spec["template"], **values)}]


def truncate(text: str) -> str:
    limit = agent_config()["llm"]["max_input_chars"]
    return text if len(text) <= limit else text[:limit] + "\n[...truncated...]"


def _count_call() -> None:
    """A project-wide daily cap, on top of the per-reviewer limits, so nothing can use up the free quota."""
    limit = agent_config()["llm"]["max_calls_per_day"]
    count = cache.count_today("gemini_calls")
    if count is not None and count > limit:
        raise DailyLimitReached(f"The daily limit of {limit} Gemini calls has been reached")


def _completion(model: str, messages: list[dict], temperature: float, max_tokens: int, task: str | None) -> str:
    cfg = agent_config()["llm"]
    try:
        with tracing.generation(task or "chat", model, messages) as record:
            response = litellm.completion(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                timeout=cfg["timeout_seconds"], num_retries=cfg["num_retries"])
            content = response.choices[0].message.content or ""
            record(content, getattr(response, "usage", None))
    except Exception as exc:  # LiteLLM raises provider-specific exception types
        error = _safe_error(exc)
        if model == cfg["chat_model"]:
            raise LLMError(f"Gemini call to {model} failed: {error}") from None
        if any(word in error.lower() for word in _DENIED):
            _denied_models.add(model)
        log.warning("Task model %s failed (%s); using %s", model, error[:200], cfg["chat_model"])
        _count_call()
        return _completion(cfg["chat_model"], messages, temperature, max_tokens, task)
    return content


def complete(messages: list[dict], *, task: str | None = None, agent: str | None = None,
             temperature: float = 0.0, max_tokens: int = 1500, use_cache: bool = True) -> str:
    """agent names a Google ADK agent (A2A) whose instruction is this call's system persona."""
    cfg = agent_config()
    model = model_for(task)
    key = cache.make_key("llm", model, messages, temperature, max_tokens)
    if use_cache:
        hit = cache.get_json(key)
        if hit is not None:
            return hit["content"]
    _count_call()
    content = None
    if agent and a2a_client.configured():
        try:
            with tracing.generation(task or agent, f"adk:{agent}_agent", messages[-1]["content"]) as record:
                content = a2a_client.send(agent, messages[-1]["content"])
                record(content)
        except a2a_client.A2AError as exc:
            log.warning("ADK %s agent unavailable (%s); calling Gemini in-process", agent, exc)
            _count_call()  # the remote agent may already have used a call
    if content is None:
        content = _completion(model, messages, temperature, max_tokens, task)
    cache.set_json(key, {"content": content}, cfg["cache"]["llm_ttl_seconds"])
    return content


def parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj


def complete_json(messages: list[dict], **kwargs) -> dict:
    """Chat call that must return a JSON object. Retries once uncached on bad output."""
    text = complete(messages, **kwargs)
    try:
        return parse_json(text)
    except ValueError:
        log.warning("Model returned invalid JSON; retrying once without cache")
        cache.delete(cache.make_key("llm", model_for(kwargs.get("task")), messages,
                                    kwargs.get("temperature", 0.0), kwargs.get("max_tokens", 1500)))
        text = complete(messages, **{**kwargs, "use_cache": False})
        try:
            return parse_json(text)
        except ValueError as exc:
            raise LLMError(f"Model did not return valid JSON: {text[:200]!r}") from exc


def embed(text: str) -> list[float]:
    """Gemini embeddings (one text per request), cached by content."""
    cfg = agent_config()
    model = cfg["llm"]["embedding_model"]
    key = cache.make_key("emb", model, text)
    hit = cache.get_json(key)
    if hit is not None:
        return hit
    _count_call()
    try:
        with tracing.generation("embedding", model, text, as_type="embedding") as record:
            response = litellm.embedding(model=model, input=[text], timeout=cfg["llm"]["timeout_seconds"])
            record(None, getattr(response, "usage", None))
    except Exception as exc:
        raise LLMError(f"Gemini embedding call to {model} failed: {_safe_error(exc)}") from None
    item = response.data[0]
    vector = item["embedding"] if isinstance(item, dict) else item.embedding
    cache.set_json(key, vector, cfg["cache"]["embedding_ttl_seconds"])
    return vector
