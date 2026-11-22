from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Owner connection: migrations and seed only.
    owner_database_url: str = "postgresql+psycopg://hms_owner:hms_owner_pw@localhost:5433/hms"
    # Restricted, RLS-bound connection used for every API request.
    app_database_url: str = "postgresql+psycopg://hms_app:hms_app_pw@localhost:5433/hms"
    app_db_password: str = "hms_app_pw"

    llm_provider: str = "groq"          # 'groq' | 'qwen'
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"

    # Optional local model: a fine-tuned Qwen served by Ollama (see training/README.md).
    # host.docker.internal reaches the host's Ollama from inside the api container.
    qwen_base_url: str = "http://host.docker.internal:11434/v1"
    qwen_model: str = "hms-intent-qwen"
    llm_timeout_seconds: int = 30

    jwt_secret: str = "dev-only-secret-change-me-in-production-0123456789"
    jwt_ttl_minutes: int = 12 * 60
    demo_mode: bool = True

    pending_ttl_minutes: int = 10      # a preview must be confirmed within this window
    typed_count_threshold: int = 50    # writes above this need the row count typed back
    sample_size: int = 5               # before/after rows shown in a preview
    max_answer_rows: int = 50          # rows returned in a read answer


@lru_cache
def get_settings() -> Settings:
    return Settings()
