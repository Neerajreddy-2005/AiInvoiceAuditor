"""Reflection agent: score an answer with the RAG Triad.

answer_relevance, groundedness and context_relevance are each scored
0..1 by the judge prompt; the answer is accepted when their average is
strictly above the configured threshold (0.7).
"""
from .. import llm

TRIAD = ("answer_relevance", "groundedness", "context_relevance")


def evaluate(question: str, context: str, answer: str) -> dict:
    if not context.strip():
        return {**{k: 0.0 for k in TRIAD}, "average": 0.0, "feedback": "No context was retrieved."}
    result = llm.complete_json(llm.prompt_messages("rag_reflection", question=question, context=context,
                                                   answer=answer), max_tokens=300)
    scores = {k: llm.unit_score(result.get(k)) for k in TRIAD}
    scores["average"] = round(sum(scores[k] for k in TRIAD) / len(TRIAD), 3)
    scores["feedback"] = str(result.get("feedback") or "")
    return scores
