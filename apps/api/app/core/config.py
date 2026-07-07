from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    project_name: str = "Wrkbase API"
    environment: str = "development"

    # Comma-separated list of origins allowed to call this API from a browser.
    cors_origins: str = "http://localhost:3000"

    # Connects as the least-privilege `wrkbase_app` role (see migration 0001),
    # never as the Postgres superuser used to run migrations.
    database_url: str

    redis_url: str

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def cookie_secure(self) -> bool:
        # Local dev runs over plain http; a `Secure` cookie would never be
        # sent back by the browser at all. Real deploys must be https.
        return self.environment != "development"

    class Config:
        env_file = ".env"


settings = Settings()
