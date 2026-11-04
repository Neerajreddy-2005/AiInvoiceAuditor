"""Question answering (LangGraph).

  route ──► sql_answer ──────────────────────────────────────────► END
    │        (structured questions: fixed, parameterised SQL on audit)
    └────► retrieve ─► augment ─► generate ─► reflect ─┬─ avg > 0.7 ──► finalize ─► END
              ▲                                         ├─ attempt 3 ──► finalize (best attempt, flagged)
              └──────────── rewrite_query ◄─────────────┘  otherwise

A repeated question is answered from Redis (rag:{sha256}) without
calling the model; the key includes the index size and the latest
indexing time, so new invoices and re-validations invalidate it.
"""
import logging
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .. import cache, llm
from .. import repository as repo
from ..settings import agent_config
from . import store
from .augmentation import build_context, rerank
from .generation import generate
from .reflection import evaluate
from .retrieval import retrieve

log = logging.getLogger(__name__)
SQL_INTENTS = {"list_invoices", "count_invoices", "sum_totals", "invoice_discrepancies"}
ANSWER_FORMAT = 2  # raise when the shape of a stored answer changes, so old ones aren't served


class RAGState(TypedDict, total=False):
    question: str
    invoice_id: str | None
    route: str
    intent: str | None
    filters: dict
    also_asked: str | None
    search_query: str
    attempt: int
    context: str
    sources: list
    hits: list
    answer: str
    scores: dict
    attempts: list
    final: dict


def _cfg() -> dict:
    return agent_config()["rag"]


def route_question(state: RAGState) -> dict:
    base = {"search_query": state["question"], "attempt": 1, "attempts": []}
    if state.get("invoice_id"):
        return {**base, "route": "rag"}
    try:
        decision = llm.complete_json(llm.prompt_messages("query_router", question=state["question"],
                                                         today=date.today().isoformat()), max_tokens=300)
    except llm.LLMError as exc:
        log.warning("Router unavailable (%s); using RAG", exc)
        decision = {}
    if decision.get("route") == "sql" and decision.get("intent") in SQL_INTENTS:
        also_asked = decision.get("also_asked")
        return {**base, "route": "sql", "intent": decision["intent"], "filters": decision.get("filters") or {},
                "also_asked": also_asked[:200] if isinstance(also_asked, str) else None}
    return {**base, "route": "rag"}


def _table(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return "No invoices match that question."
    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0].replace('_', ' ').capitalize()}: {rows[0][columns[0]]}"
    head = "| " + " | ".join(c.replace("_", " ") for c in columns) + " |"
    sep = "|" + "---|" * len(columns)
    body = ["| " + " | ".join("" if r[c] is None else str(r[c]) for c in columns) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def _notes(state: RAGState, rows: list[dict]) -> list[str]:
    """What a table can't say by itself. A fixed query answers one thing, so say what it left out."""
    notes = []
    if state["intent"] == "sum_totals":
        notes.append("These are amounts vendors billed, not revenue."
                     + ("" if (state.get("filters") or {}).get("recommendation")
                        else " They include flagged and rejected invoices."))
        no_currency = sum(int(r["invoices"]) for r in rows if r.get("currency") is None)
        if no_currency:
            notes.append(f"Invoices with no currency on record: {no_currency}. They may be in different currencies, "
                         "so they aren't added up. Correct the currency on their review pages to include them.")
    if state.get("also_asked"):
        notes.append(f"Not answered here: {state['also_asked']}. Please ask that separately.")
    return notes


def sql_answer(state: RAGState) -> dict:
    try:
        columns, rows = repo.structured_query(state["intent"], state.get("filters") or {})
    except Exception as exc:
        log.warning("Structured query failed (%s); falling back to RAG", exc)
        return {"route": "rag"}
    return {"answer": _table(columns, rows),
            "final": {"route": "sql", "intent": state["intent"], "filters": state.get("filters"),
                      "answer": _table(columns, rows), "rows": rows, "notes": _notes(state, rows), "passed": True,
                      "scores": None, "attempts": [], "sources": []}}


def retrieve_node(state: RAGState) -> dict:
    cfg = _cfg()
    top_k = cfg["top_k"] + (state["attempt"] - 1) * cfg["top_k_step"]
    return {"hits": retrieve(state["search_query"], top_k=top_k, invoice_id=state.get("invoice_id"))}


def augment_node(state: RAGState) -> dict:
    query = state["question"] if state["search_query"] == state["question"] \
        else f"{state['question']} {state['search_query']}"
    ranked = rerank(query, state["hits"], _cfg()["rerank_keep"])
    context, sources = build_context(ranked)
    return {"context": context, "sources": sources}


def generate_node(state: RAGState) -> dict:
    return {"answer": generate(state["question"], state["context"])}


def reflect_node(state: RAGState) -> dict:
    scores = evaluate(state["question"], state["context"], state["answer"])
    attempt = {"attempt": state["attempt"], "query": state["search_query"], "answer": state["answer"],
               "scores": scores, "sources": state["sources"]}
    log.info("RAG attempt %s scored %.3f", state["attempt"], scores["average"])
    return {"scores": scores, "attempts": state["attempts"] + [attempt]}


def after_reflect(state: RAGState) -> str:
    cfg = _cfg()
    if state["scores"]["average"] > float(cfg["pass_threshold"]):
        return "finalize"
    if state["attempt"] >= int(cfg["max_attempts"]):
        return "finalize"
    return "rewrite_query"


def rewrite_query(state: RAGState) -> dict:
    try:
        result = llm.complete_json(llm.prompt_messages("query_rewrite", question=state["question"],
                                                       feedback=state["scores"].get("feedback", "")),
                                   max_tokens=200)
        query = str(result.get("query") or state["question"])
    except llm.LLMError:
        query = state["question"]
    return {"search_query": query, "attempt": state["attempt"] + 1}


def finalize(state: RAGState) -> dict:
    threshold = float(_cfg()["pass_threshold"])
    best = max(state["attempts"], key=lambda a: a["scores"]["average"])
    passed = best["scores"]["average"] > threshold
    answer = best["answer"]
    if not passed:
        answer += (f"\n\nLow confidence: this answer scored {best['scores']['average']:.2f} on the RAG Triad "
                   f"(threshold {threshold}) after {len(state['attempts'])} attempts. Please verify it.")
    return {"final": {"route": "rag", "answer": answer, "passed": passed, "scores": best["scores"],
                      "sources": best["sources"], "attempts": [
                          {"attempt": a["attempt"], "query": a["query"], "scores": a["scores"]}
                          for a in state["attempts"]]}}


def build_graph():
    g = StateGraph(RAGState)
    g.add_node("route_question", route_question)
    g.add_node("sql_answer", sql_answer)
    g.add_node("retrieve", retrieve_node)
    g.add_node("augment", augment_node)
    g.add_node("generate", generate_node)
    g.add_node("reflect", reflect_node)
    g.add_node("rewrite_query", rewrite_query)
    g.add_node("finalize", finalize)

    g.add_edge(START, "route_question")
    g.add_conditional_edges("route_question", lambda s: "sql_answer" if s["route"] == "sql" else "retrieve",
                            ["sql_answer", "retrieve"])
    g.add_conditional_edges("sql_answer", lambda s: END if s.get("final") else "retrieve", [END, "retrieve"])
    g.add_edge("retrieve", "augment")
    g.add_edge("augment", "generate")
    g.add_edge("generate", "reflect")
    g.add_conditional_edges("reflect", after_reflect, ["finalize", "rewrite_query"])
    g.add_edge("rewrite_query", "retrieve")
    g.add_edge("finalize", END)
    return g.compile()


_graph = None


def answer_query(question: str, invoice_id: str | None = None) -> dict:
    global _graph
    if _graph is None:
        _graph = build_graph()
    question = question.strip()
    key = cache.make_key("rag", ANSWER_FORMAT, question.lower(), invoice_id, store.point_count(), repo.last_indexed_at())
    cached = cache.get_json(key)
    if cached:
        return {**cached, "cached": True}
    out = _graph.invoke({"question": question, "invoice_id": invoice_id}, {"recursion_limit": 40})
    final = out["final"]
    if final["route"] == "rag" and final["passed"]:
        cache.set_json(key, final, agent_config()["cache"]["rag_answer_ttl_seconds"])
    return {**final, "cached": False}
