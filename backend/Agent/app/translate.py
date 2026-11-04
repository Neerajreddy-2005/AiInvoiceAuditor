"""Translation agent: detect language, translate to English via Gemini, cache.

Confidence = min(model's self-reported confidence, share of the source's
numeric tokens that survive translation). A translation that drops or
alters an amount cannot score high, whatever the model claims.
"""
import logging
import re

from langdetect import DetectorFactory, LangDetectException, detect_langs

from . import cache, llm
from .settings import agent_config

log = logging.getLogger(__name__)
DetectorFactory.seed = 0  # deterministic detection


def detect_language(text: str, hint: str | None = None) -> tuple[str | None, float]:
    letters = re.sub(r"[^A-Za-zÀ-ÿ\s]", " ", text)
    try:
        candidates = detect_langs(letters)
    except LangDetectException:
        return (hint, 0.0) if hint else (None, 0.0)
    best = candidates[0]
    if best.prob >= agent_config()["translation"]["min_detection_probability"] or not hint:
        return best.lang, best.prob
    return hint, best.prob


def numeric_preservation(source: str, translated: str) -> float:
    numbers = re.findall(r"\d+(?:[.,]\d+)*", source)
    if not numbers:
        return 1.0
    # Whole tokens: 1000.00 is not preserved by 11000.00.
    translated_numbers = set(re.findall(r"\d+(?:[.,]\d+)*", translated))
    kept = sum(1 for n in numbers if n in translated_numbers)
    return kept / len(numbers)


def translate(text: str, language_hint: str | None = None) -> dict:
    """Returns {source_language, translated_text, confidence, method}."""
    if not text.strip():
        return {"source_language": None, "translated_text": "", "confidence": 0.0, "method": "empty"}

    language, probability = detect_language(text, language_hint)
    # Neither a sidecar hint nor a weak guess can prove English: detection must be confident.
    min_probability = agent_config()["translation"]["min_detection_probability"]
    if language == "en" and probability >= min_probability:
        return {"source_language": "en", "translated_text": text, "confidence": 1.0,
                "method": f"not required (langdetect p={probability:.2f})"}

    source = llm.truncate(text)
    cfg = agent_config()
    key = cache.make_key("translation", cfg["llm"]["chat_model"], source)
    cached = cache.get_json(key)
    if isinstance(cached, dict):
        log.info("Translation cache hit")
        # Re-check on read: the cache is not a trusted source for a decision input.
        preserved = numeric_preservation(source, str(cached.get("translated_text") or ""))
        confidence = min(llm.unit_score(cached.get("confidence", 0)), preserved)
        return {**cached, "confidence": round(confidence, 3), "method": "cache"}

    result = llm.complete_json(
        llm.prompt_messages("translation", text=source, language_hint=language or ""),
        max_tokens=4000)
    translated = str(result.get("translated_text") or "").strip()
    if not translated:
        raise llm.LLMError("Translation returned no text")
    model_conf = llm.unit_score(result.get("confidence", 0))
    preserved = numeric_preservation(source, translated)
    detected = str(result.get("detected_language") or language or "").lower()[:8] or None
    output = {
        "source_language": detected,
        "translated_text": translated,
        "confidence": round(min(model_conf, preserved), 3),
        "model_confidence": model_conf,
        "numeric_preservation": round(preserved, 3),
    }
    cache.set_json(key, output, cfg["cache"]["translation_ttl_seconds"])
    return {**output, "method": "gemini"}
