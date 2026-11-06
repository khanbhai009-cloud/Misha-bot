"""Runtime configuration — every value comes from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    bot_token: str
    webhook_base_url: str
    webhook_secret: str
    database_url: str
    nvidia_api_key: str = ""
    nvidia_model: str = "meta/llama-3.2-90b-vision-instruct"
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    llm_provider_order: str = "nvidia,openrouter,groq"
    llm_cooldown_minutes: int = 60
    admin_id: int = 0
    admin_panel_key: str = ""
    llm_max_parallel: int = 10
    voice_replies: str = "off"
    port: int = 7860
    polling: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            webhook_base_url=os.getenv("WEBHOOK_BASE_URL", "").rstrip("/"),
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            database_url=os.getenv("DATABASE_URL", ""),
            nvidia_api_key=os.getenv("NVIDIA_API_KEY", ""),
            nvidia_model=os.getenv("NVIDIA_MODEL", "meta/llama-3.2-90b-vision-instruct"),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
            llm_provider_order=os.getenv(
                "LLM_PROVIDER_ORDER", "nvidia,openrouter,groq"
            ),
            llm_cooldown_minutes=int(os.getenv("LLM_COOLDOWN_MINUTES", "60") or 60),
            admin_id=int(os.getenv("ADMIN_ID", "0") or 0),
            admin_panel_key=os.getenv("ADMIN_PANEL_KEY", ""),
            llm_max_parallel=int(os.getenv("LLM_MAX_PARALLEL", "10") or 10),
            voice_replies=os.getenv("VOICE_REPLIES", "off").strip().lower(),
            port=int(os.getenv("PORT", "7860") or 7860),
            polling=os.getenv("POLLING", "0") == "1",
        )

    def missing_for_run(self) -> list:
        """Env vars that must be set before the bot can actually run."""
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.database_url:
            missing.append("DATABASE_URL")
        if not self.polling and not self.webhook_base_url:
            missing.append("WEBHOOK_BASE_URL")
        return missing


_settings = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
