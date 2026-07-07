from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class SignupRequest(BaseModel):
    # Both optional at the field level; the validator below enforces the
    # real either/or rule. Two genuinely different signup paths share one
    # endpoint: no invite_token creates a brand-new org (org_name required),
    # a valid invite_token joins that invite's existing org instead
    # (org_name ignored — the org is whatever the invite says).
    org_name: str | None = Field(default=None, min_length=1)
    invite_token: str | None = None
    email: EmailStr
    password: str = Field(min_length=8)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        # Emails are matched byte-for-byte against user_lookup/users; without
        # normalizing case, "Alice@Acme.test" at signup and "alice@acme.test"
        # at login would be treated as two different, non-matching values.
        return value.lower()

    @model_validator(mode="after")
    def require_org_name_without_invite(self) -> "SignupRequest":
        if not self.invite_token and not self.org_name:
            raise ValueError("org_name is required when signing up without an invite token")
        return self


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class RefreshRequest(BaseModel):
    # Optional: the browser flow relies on the httpOnly refresh_token cookie
    # instead (see app/api/auth.py), since client-side JS never has the
    # value to put in a body. Bearer-token API clients still send it here.
    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
