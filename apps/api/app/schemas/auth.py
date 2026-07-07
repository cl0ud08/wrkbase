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
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
