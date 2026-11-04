"""Retrieval agent: embed the query and search Qdrant, optionally scoped to one invoice."""
from qdrant_client.models import FieldCondition, Filter, MatchValue

from .. import llm
from . import store


def retrieve(query: str, *, top_k: int, invoice_id: str | None = None,
             source_type: str | None = None) -> list[dict]:
    must = []
    if invoice_id:
        must.append(FieldCondition(key="invoice_id", match=MatchValue(value=invoice_id)))
    if source_type:
        must.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))
    store.ensure_collection()
    result = store.client().query_points(
        store.collection_name(), query=llm.embed(query), query_filter=Filter(must=must) if must else None,
        limit=top_k, with_payload=True)
    return [{"id": str(p.id), "score": float(p.score), "payload": p.payload} for p in result.points]
