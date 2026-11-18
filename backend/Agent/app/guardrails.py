"""Responsible-AI guardrails.

Deterministic checks around the model, so they cannot be talked out of:

  input   check_question()   refuses empty, oversized or prompt-injection questions
          find_injection()   flags vendor documents that try to instruct the model;
                             the invoice is routed to manual review (never auto-approved)
  output  redact_pii()       masks IBANs and card numbers in answers shown to people

Other guardrails live where they act:
  - personas tell every model that document text is data, not instructions (prompts.yaml)
  - the recommendation is computed by rules, never by the model (report.recommend)
  - recovered fields are kept only if literally printed in the invoice (validation)
  - translation confidence is capped by how many numbers survive (translate)
  - the router only picks fixed, parameterised SQL templates; the model never writes SQL
  - RAG answers must pass the RAG Triad or are flagged as low confidence (rag.graph)
"""
import re

MAX_QUESTION_CHARS = 1000

_INJECTION = [re.compile(p, re.IGNORECASE) for p in (
    r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
    r"(?:instructions?|prompts?|rules|messages?)",
    r"\bdisregard\s+(?:all\s+|the\s+|any\s+)?(?:previous|prior|above|earlier|your)\b",
    r"\bforget\s+(?:all\s+|your\s+)?(?:previous\s+)?instructions\b",
    r"\byou\s+are\s+now\s+(?:a|an|the)\b",
    r"\b(?:system|developer)\s+(?:prompt|message|instructions?)\b",
    # Approval wording only when aimed at the AI: vendors write "please approve this invoice" too.
    r"\bauto[- ]?approve\b",
    r"\b(?:you|assistant|ai|model|llm)\s+(?:must|should|will|shall|have\s+to)\s+"
    r"(?:approve|accept|mark|pass|validate)\b",
    r"\bdo\s+not\s+(?:flag|report|validate|audit)\b",
    r"<\s*/?\s*(?:script|system|instructions?)\b",
    r"\bjailbreak\b",
)]

_IBAN = re.compile(r"\b([A-Z]{2}\d{2})((?:[ ]?[A-Z0-9]{4}){2,7})([ ]?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def find_injection(text: str) -> list[str]:
    """Snippets of text that look like instructions aimed at the model."""
    hits = []
    for pattern in _INJECTION:
        m = pattern.search(text or "")
        if m:
            hits.append(m.group(0))
    return hits[:3]


def check_question(question: str) -> str | None:
    """None if the question may go to the model, else the reason it was refused."""
    if not question.strip():
        return "Please type a question."
    if len(question) > MAX_QUESTION_CHARS:
        return f"Questions are limited to {MAX_QUESTION_CHARS} characters."
    if find_injection(question):
        return ("This question looks like an attempt to change the assistant's instructions, so it was "
                "not sent to the model. Ask about the processed invoices instead.")
    return None


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def redact_pii(text: str) -> str:
    """Mask IBANs and payment-card numbers, keeping the last 4 characters."""
    if not text:
        return text

    def iban(m: re.Match) -> str:
        raw = re.sub(r"\s", "", m.group(0))
        return f"{raw[:2]}** **** {raw[-4:]}" if len(raw) >= 15 else m.group(0)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return f"**** **** **** {digits[-4:]}" if _luhn(digits) else m.group(0)

    return _CARD.sub(card, _IBAN.sub(iban, text))
