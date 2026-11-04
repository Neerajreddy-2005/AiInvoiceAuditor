"""Qdrant connection and collection setup (single collection)."""
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

from ..settings import agent_config, settings

_client: QdrantClient | None = None

PAYLOAD_INDEXES = ("invoice_id", "source_type", "vendor", "language")


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=settings().qdrant_url, api_key=settings().qdrant_api_key, timeout=10)
    return _client


def collection_name() -> str:
    return agent_config()["rag"]["collection"]


def ensure_collection(recreate: bool = False) -> None:
    c, name = client(), collection_name()
    if recreate and c.collection_exists(name):
        c.delete_collection(name)
    if not c.collection_exists(name):
        c.create_collection(name, vectors_config=VectorParams(
            size=int(agent_config()["llm"]["embedding_dimensions"]), distance=Distance.COSINE))
        for field in PAYLOAD_INDEXES:
            c.create_payload_index(name, field_name=field, field_schema=PayloadSchemaType.KEYWORD)


def point_count() -> int:
    c, name = client(), collection_name()
    if not c.collection_exists(name):
        return 0
    return c.count(name, exact=True).count
