"""MCP server: the auditor's tools over the Model Context Protocol.

Served at http://127.0.0.1:8100/mcp/ inside the agent container only
(streamable HTTP, stateless). It is never published, so the frontend and
the demo tunnel can't reach it: some tools read local files. The LangGraph
nodes discover these tools with tools/list and invoke them with tools/call
through app.mcp_client.

Tools compute and return JSON; the nodes that call them persist the
results, so all audit writes stay in the workflow nodes.

  Invoice agents               RAG agents
  invoice-watcher              vector-indexer
  data-harvester               semantic-retriever
  lang-bridge                  chunk-ranker
  data-completeness-checker    response-synthesizer
  business-validation          rag-evaluator
  insight-reporter
"""
import logging
import threading
from functools import partial

import anyio
import uvicorn
from mcp.server.fastmcp import Context, FastMCP

from . import tracing, translate, workflow
from .rag import augmentation, generation, indexing, reflection, retrieval

log = logging.getLogger(__name__)
HOST, PORT = "127.0.0.1", 8100

mcp = FastMCP("invoice-auditor-tools", stateless_http=True, json_response=True, streamable_http_path="/mcp/")


async def _run(ctx: Context, name: str, fn, /, *args, **kwargs):
    """Run a blocking tool in a worker thread (keeps /health responsive), as a span of the caller's trace."""
    meta = ctx.request_context.meta
    meta = meta.model_dump() if meta is not None else None
    return await anyio.to_thread.run_sync(partial(tracing.continue_trace, f"mcp:{name}",
                                                  partial(fn, *args, **kwargs), meta))


# ---------------------------------------------------------------- invoice agents

@mcp.tool(name="invoice-watcher", description="List invoice files in the incoming folder that are not yet in "
          "the audit trail (identified by SHA-256 of the content). Read-only.")
async def invoice_watcher(ctx: Context) -> dict:
    from .monitor_agent import monitor  # import here: monitor imports the workflow
    return await _run(ctx, "invoice-watcher", lambda: {"files": monitor.detect_new_files()})


@mcp.tool(name="data-harvester", description="Extract the full text of a PDF, DOCX or image invoice "
          "(direct text first, Tesseract OCR when there is no text layer) and parse header fields and line items.")
async def data_harvester(ctx: Context, file_path: str, file_type: str) -> dict:
    return await _run(ctx, "data-harvester", lambda: workflow.harvest(file_path, file_type))


@mcp.tool(name="lang-bridge", description="Detect the language of invoice text and translate it to English "
          "(via the Google ADK translation agent over A2A), with a confidence score. Results are cached.")
async def lang_bridge(ctx: Context, text: str, language_hint: str | None = None) -> dict:
    return await _run(ctx, "lang-bridge", lambda: translate.translate(text, language_hint))


@mcp.tool(name="data-completeness-checker", description="Check an invoice against configs/rules.yaml: "
          "required fields, types, accepted currencies, arithmetic, duplicates, and text aimed at the AI.")
async def data_completeness_checker(ctx: Context, invoice_id: str, invoice: dict, line_items: list[dict],
                                    document_text: str, recovered_fields: dict, corrected: list[str],
                                    first_read: bool) -> dict:
    return await _run(ctx, "data-completeness-checker", lambda: workflow.check_data(
        invoice_id, invoice, line_items, document_text, recovered_fields, corrected, first_read))


@mcp.tool(name="business-validation", description="Cross-check an invoice against the ERP (mock-erp HTTP "
          "API: PO, vendor, SKU) with the tolerances in rules.yaml; one discrepancy per mismatched field.")
async def business_validation(ctx: Context, invoice: dict, line_items: list[dict]) -> dict:
    return await _run(ctx, "business-validation", lambda: workflow.check_business(invoice, line_items))


@mcp.tool(name="insight-reporter", description="Build the audit report: rule-based recommendation "
          "(approve / review / reject), discrepancy summary, an AI summary (Google ADK reporting agent "
          "over A2A) and the HTML rendering.")
async def insight_reporter(ctx: Context, audit: dict, ai_translated: bool) -> dict:
    return await _run(ctx, "insight-reporter", lambda: workflow.build_audit_report(audit, ai_translated))


# ---------------------------------------------------------------- RAG agents

@mcp.tool(name="vector-indexer", description="Chunk an invoice's stored text and latest report (read from "
          "PostgreSQL), embed the chunks and upsert them into Qdrant.")
async def vector_indexer(ctx: Context, invoice_id: str) -> dict:
    return await _run(ctx, "vector-indexer", lambda: {"points": indexing.index_invoice(invoice_id)})


@mcp.tool(name="semantic-retriever", description="Embed a query and return the most similar chunks from "
          "Qdrant, optionally restricted to one invoice.")
async def semantic_retriever(ctx: Context, query: str, top_k: int, invoice_id: str | None = None) -> dict:
    return await _run(ctx, "semantic-retriever",
                      lambda: {"hits": retrieval.retrieve(query, top_k=top_k, invoice_id=invoice_id)})


def _rank(question: str, hits: list[dict], keep: int) -> dict:
    context, sources = augmentation.build_context(augmentation.rerank(question, hits, keep))
    return {"context": context, "sources": sources}


@mcp.tool(name="chunk-ranker", description="Rerank retrieved chunks (vector score and query-term coverage), "
          "drop duplicates and assemble numbered context passages.")
async def chunk_ranker(ctx: Context, question: str, hits: list[dict], keep: int) -> dict:
    return await _run(ctx, "chunk-ranker", _rank, question, hits, keep)


@mcp.tool(name="response-synthesizer", description="Answer a question using only the given context "
          "passages, with citations.")
async def response_synthesizer(ctx: Context, question: str, context: str) -> dict:
    return await _run(ctx, "response-synthesizer", lambda: {"answer": generation.generate(question, context)})


@mcp.tool(name="rag-evaluator", description="Score an answer with the RAG Triad: answer relevance, "
          "groundedness and context relevance (0..1) and their average.")
async def rag_evaluator(ctx: Context, question: str, context: str, answer: str) -> dict:
    return await _run(ctx, "rag-evaluator", lambda: reflection.evaluate(question, context, answer))


TOOL_NAMES = ("invoice-watcher", "data-harvester", "lang-bridge", "data-completeness-checker",
              "business-validation", "insight-reporter", "vector-indexer", "semantic-retriever",
              "chunk-ranker", "response-synthesizer", "rag-evaluator")


def start() -> uvicorn.Server:
    """Serve MCP on loopback in a background thread, beside the public API."""
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host=HOST, port=PORT,
                                           log_level="warning", lifespan="on"))
    threading.Thread(target=server.run, name="mcp-server", daemon=True).start()
    return server
