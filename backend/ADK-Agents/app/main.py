"""Google ADK agents served over the Agent-to-Agent (A2A) protocol. Port 8002.

Two of the auditor's agents are built with Google ADK instead of LangGraph:

  translation_agent  POST /translation   card: /translation/.well-known/agent-card.json
  reporting_agent    POST /reporting     card: /reporting/.well-known/agent-card.json

The agent service discovers them by their agent cards and calls them with
A2A JSON-RPC. They are stateless: they return text, and the agent service
caches it, counts it toward the daily Gemini limit and persists it, so the
agent stays the only writer of application data.

Models are native Gemini (google-genai, GOOGLE_API_KEY); personas come from
configs/prompts.yaml and models from configs/agent.yaml (task_models).
"""
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import yaml
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import LlmAgent
from google.genai import types
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(stream=sys.stdout, level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s [adk-agents] %(name)s: %(message)s")
# Request logs would otherwise include Gemini URLs.
for noisy in ("httpx", "httpcore", "google_genai", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("adk-agents")

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/app/configs"))
PUBLIC_HOST = os.getenv("A2A_PUBLIC_HOST", "adk-agents")  # the name other containers resolve
PORT = int(os.getenv("PORT", "8002"))


def _yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        sys.exit(f"adk-agents: config {path} not found (is ./backend/configs mounted?)")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _model(cfg: dict, task: str) -> str:
    """The task's model from agent.yaml, as ADK names it (no LiteLLM 'gemini/' prefix).

    Resolved the same way as the agent's settings.agent_config(): CHAT_MODEL also moves tasks on the default.
    """
    llm = cfg["llm"]
    default = llm["chat_model"]
    chat = os.getenv("CHAT_MODEL") or default
    model = (llm.get("task_models") or {}).get(task) or default
    return (chat if model == default else model).removeprefix("gemini/")


def build_agents() -> tuple[LlmAgent, LlmAgent]:
    cfg, prompts = _yaml("agent.yaml"), _yaml("prompts.yaml")
    personas = prompts["personas"]
    translation = LlmAgent(
        name="translation_agent",
        description="Translates invoice text from any language into English and returns JSON with "
                    "detected_language, translated_text and a 0..1 confidence score.",
        model=_model(cfg, "translation"),
        instruction=personas["translator"].strip(),
        generate_content_config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=4000),
    )
    reporting = LlmAgent(
        name="reporting_agent",
        description="Writes a short plain-text audit summary of an invoice validation result for a "
                    "finance reviewer.",
        model=_model(cfg, "report_summary"),
        instruction=personas["auditor"].strip(),
        generate_content_config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=400),
    )
    return translation, reporting


def create_app() -> Starlette:
    if not os.getenv("GOOGLE_API_KEY", "").strip():
        sys.exit("adk-agents: missing GOOGLE_API_KEY (compose passes GEMINI_API_KEY from .env).")
    agents = build_agents()
    # Each to_a2a app's lifespan builds the agent card and attaches its A2A
    # routes (prefixed by rpc_path) to the app it is given, so running both
    # lifespans against one root app hosts both agents on one port.
    a2a_apps = [to_a2a(agent, host=PUBLIC_HOST, port=PORT, rpc_path=agent.name.removesuffix("_agent"))
                for agent in agents]

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            for sub in a2a_apps:
                await stack.enter_async_context(sub.router.lifespan_context(app))
            log.info("A2A agents ready: %s", ", ".join(f"{a.name} ({a.model})" for a in agents))
            yield

    async def health(_):
        return JSONResponse({"status": "ok"})

    return Starlette(routes=[Route("/health", health)], lifespan=lifespan)


app = create_app()
