"""Runtime configuration.

All settings come from environment variables or the project's ``.env`` file.
Names are case-insensitive and match the field names below (``NCBI_API_KEY`` ->
``ncbi_api_key``). Connectors that need a setting not declared here read it with
:meth:`Settings.env`, so adding a new source never requires editing this file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Claude ---
    anthropic_api_key: str | None = None
    mosaic_model: str = "claude-opus-5-5"
    mosaic_effort: str = "high"
    mosaic_max_tokens: int = 64000
    mosaic_enable_web_tools: bool = True
    mosaic_tool_loading: str = "deferred"  # "deferred" (tool search) | "eager"
    mosaic_max_agent_turns: int = 40

    # --- Keyed sources ---
    ncbi_api_key: str | None = None
    semantic_scholar_api_key: str | None = None
    openalex_api_key: str | None = None
    elsevier_scopus_api_key: str | None = None
    elsevier_insttoken: str | None = None
    springer_openaccess_api_key: str | None = None
    springer_meta_api_key: str | None = None
    openfda_api_key: str | None = None
    clarivate_api_key: str | None = None

    # --- DrugCentral (public read-only PostgreSQL) ---
    drugcentral_pg_host: str = "unmtid-dbs.net"
    drugcentral_pg_port: int = 5433
    drugcentral_pg_dbname: str = "drugcentral"
    drugcentral_pg_user: str = "drugman"
    drugcentral_pg_password: str = "dosage"

    # --- Application ---
    biopha_contact_email: str | None = None
    biopha_data_dir: Path = Path("./data")
    biopha_audit_db: Path = Path("./data/audit.db")
    biopha_cache_db: Path = Path("./data/cache.db")
    biopha_cache_ttl: int = 21600
    biopha_http_timeout: float = 30.0
    biopha_log_level: str = "INFO"
    biopha_api_host: str = "127.0.0.1"
    biopha_api_port: int = 8000

    # ------------------------------------------------------------------
    def env(self, name: str, default: str | None = None) -> str | None:
        """Read an arbitrary setting (process env first, then ``.env``).

        Empty strings count as unset.
        """
        for source in (os.environ, _dotenv()):
            for key in (name, name.upper(), name.lower()):
                value = source.get(key)
                if value not in (None, ""):
                    return value
        return default

    def _resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def data_dir(self) -> Path:
        path = self._resolve(self.biopha_data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def audit_db_path(self) -> Path:
        path = self._resolve(self.biopha_audit_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cache_db_path(self) -> Path:
        path = self._resolve(self.biopha_cache_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def exports_dir(self) -> Path:
        path = self.data_dir / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str | None]:
    return dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
