"""Централизованные настройки приложения.

Значения читаются из переменных окружения / .env. Один и тот же код работает
и локально, и на сервере — разница только в содержимом .env (см. .env.example).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "local"  # "local" или "prod"

    # --- Postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "tennis"
    postgres_password: str = "tennis"
    postgres_db: str = "tennis"

    # --- Yandex S3 (S3-совместимое хранилище) ---
    s3_endpoint_url: str = "https://storage.yandexcloud.net"
    s3_bucket: str = "pelmen-data-storage"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "ru-central1"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
