from pydantic import BaseModel, EmailStr, Field, field_validator


class SignupRequest(BaseModel):
    org_name: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=8)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        # Emails are matched byte-for-byte against user_lookup/users; without
        # normalizing case, "Alice@Acme.test" at signup and "alice@acme.test"
        # at login would be treated as two different, non-matching values.
        return value.lower()


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
