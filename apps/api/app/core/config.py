from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    project_name: str = "Wrkbase API"
    environment: str = "development"

    # Comma-separated list of origins allowed to call this API from a browser.
    cors_origins: str = "http://localhost:3000"

    # Kept distinct from cors_origins (a security allowlist that could
    # legitimately hold several origins) even though they're the same
    # value today: this one answers a different question — "where do I
    # build a link that points back at the frontend" — for the invite
    # link returned by POST /invites.
    frontend_url: str = "http://localhost:3000"
    invite_expire_days: int = 7
    # Deliberately much shorter than an invite: a password-reset token is
    # meant to be used within minutes of requesting it, not shared or
    # revisited days later — the shorter the window, the smaller a leaked
    # or intercepted reset link's blast radius.
    password_reset_expire_minutes: int = 45
    # Much longer than password reset, deliberately: verification is a
    # soft nudge, not a security-sensitive credential grant (see
    # User.is_verified's docstring) — there's no comparable blast-radius
    # argument for a short window, and a real inbox check often doesn't
    # happen within the hour, let alone within minutes.
    email_verification_expire_hours: int = 48

    # Connects as the least-privilege `wrkbase_app` role (see migration 0001),
    # never as the Postgres superuser used to run migrations.
    database_url: str

    redis_url: str
    rabbitmq_url: str

    # Empty default, not required: some deploys of this app (a bare
    # `worker` running the Slice-1 plumbing only, before anyone's added a
    # key) shouldn't fail to boot over it. Never logged, never included in
    # any response — see app/services/llm_triage.py for the only place
    # these are read at all.
    groq_api_key: str = ""
    gemini_api_key: str = ""
    # None = use each SDK's own real default endpoint. Overridden only by
    # CI's plumbing-only proof run, pointed at a local stub server instead
    # of the real providers — see scripts/_fake_llm_server.py and
    # verify_triage.py for why: this is a completely ordinary, SDK-
    # supported config knob (both AsyncGroq and genai.Client accept a
    # custom base URL natively), not a special "mock mode" branch grafted
    # onto production code.
    groq_base_url: str | None = None
    gemini_base_url: str | None = None

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
