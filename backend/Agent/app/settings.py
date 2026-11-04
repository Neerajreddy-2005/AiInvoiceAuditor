"""Configuration.

Secrets and service hostnames come from the environment (supplied by
compose from .env at runtime). Business rules, runtime tuning and
prompts come from the YAML files in configs/, mounted read-only.
"""
import os
from functools import lru_cache
from pathlib import Path

import yaml
from psycopg.conninfo import make_conninfo

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/app/configs"))


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}. "
                           f"Set it in .env (see .env.example).")
    return value


def _require_secret(name: str) -> str:
    """The placeholders in .env.example and setup.sql are public, so they are refused."""
    value = _require(name)
    if value.startswith(("change-me", "REPLACE_WITH")):
        raise RuntimeError(f"{name} still has its placeholder value. Set a real secret in .env.")
    return value


class Settings:
    def __init__(self) -> None:
        self.db_host = _require("DB_HOST")
        self.db_port = int(os.getenv("DB_PORT", "5432"))
        self.db_name = _require("POSTGRES_DB")
        self.db_user = _require("AGENT_DB_USER")
        self.db_password = _require_secret("AGENT_DB_PASSWORD")
        self.db_sslrootcert = os.getenv("DB_SSLROOTCERT") or None
        self.supabase_url = _require("SUPABASE_URL").rstrip("/")
        self.reviewer_emails = frozenset(e.strip().lower() for e in _require("REVIEWER_EMAILS").split(",") if e.strip())

        self.erp_base_url = os.getenv("ERP_BASE_URL", "http://mock-erp:8000").rstrip("/")
        self.erp_service_token = _require_secret("ERP_SERVICE_TOKEN")
        self.qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
        self.qdrant_api_key = _require_secret("QDRANT_API_KEY")
        self.redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

        self.incoming_dir = Path(os.getenv("INCOMING_DIR", "/data/incoming"))
        self.reports_dir = Path(os.getenv("REPORTS_DIR", "/data/reports"))
        self.log_level = os.getenv("LOG_LEVEL", "INFO")

    def conninfo(self, search_path: str | None = None) -> str:
        extra = {"options": f"-c search_path={search_path}"} if search_path else {}
        # With Supabase's CA certificate the server's identity is verified as well as encrypted.
        tls = {"sslmode": "verify-full", "sslrootcert": self.db_sslrootcert} if self.db_sslrootcert \
            else {"sslmode": "require"}
        return make_conninfo(host=self.db_host, port=self.db_port, dbname=self.db_name,
                             user=self.db_user, password=self.db_password,
                             connect_timeout=10, application_name="agent", **tls, **extra)

    @staticmethod
    def check_gemini_credentials() -> None:
        """Gemini is reached through LiteLLM, which reads GEMINI_API_KEY."""
        if not os.getenv("GEMINI_API_KEY", "").strip():
            raise RuntimeError("Gemini API key is not configured: missing GEMINI_API_KEY. "
                               "Set it in .env, save the file, then run "
                               "`docker compose up -d` (a plain restart does not reload .env).")
        for name in ("CHAT_MODEL", "EMBED_MODEL"):
            model = os.getenv(name)
            if model and not model.startswith("gemini/"):
                raise RuntimeError(f"{name}={model!r} must start with 'gemini/' (a forward slash), "
                                   "e.g. gemini/gemini-3.5-flash-lite")


@lru_cache
def settings() -> Settings:
    return Settings()


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise RuntimeError(f"Config file {path} not found (is ./configs mounted?)")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache
def rules() -> dict:
    return _load_yaml("rules.yaml")


@lru_cache
def agent_config() -> dict:
    cfg = _load_yaml("agent.yaml")
    llm = cfg.setdefault("llm", {})
    llm["chat_model"] = os.getenv("CHAT_MODEL") or llm["chat_model"]
    llm["embedding_model"] = os.getenv("EMBED_MODEL") or llm["embedding_model"]
    return cfg


@lru_cache
def prompts() -> dict:
    return _load_yaml("prompts.yaml")
