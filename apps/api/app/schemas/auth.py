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


class PasswordResetRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class PasswordResetRequestResponse(BaseModel):
    # Identical in shape and content whether or not the email exists —
    # see request_password_reset in app/api/auth.py for the full
    # enumeration-prevention reasoning. reset_link is a temporary dev-mode
    # stand-in for real email delivery (out of scope, same limitation as
    # invites) — it is NOT how this would work in production, where the
    # link would be emailed out-of-band and never appear in this response
    # at all, existing or not.
    message: str = "If an account exists for this email, a password reset link has been generated."
    reset_link: str


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class PasswordResetConfirmResponse(BaseModel):
    message: str = "Password has been reset. Please log in with your new password."


class SignupResponse(TokenPair):
    # None for an invite-redeeming signup (see signup() in app/api/auth.py
    # — an invite already binds the account to a specific, admin-vouched-
    # for email, so no separate verification step exists for that path at
    # all) or if the response is otherwise not applicable. Populated for a
    # brand-new self-serve org's first user — same dev-mode-only stand-in
    # for real email delivery as PasswordResetRequestResponse.reset_link,
    # not how this would work in production.
    verification_link: str | None = None


class EmailVerifyRequest(BaseModel):
    token: str


class EmailVerifyResponse(BaseModel):
    message: str = "Email verified."


class ResendVerificationRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class WsTicketResponse(BaseModel):
    # 30-second single-use credential for a WebSocket handshake — see
    # app/services/ws_tickets.py for why this exists instead of the actual
    # access token.
    ticket: str


class ResendVerificationResponse(BaseModel):
    # Same enumeration-safety shape as PasswordResetRequestResponse, and
    # for the same reason — this is public/unauthenticated and must not
    # become an oracle for "is this email registered." See
    # resend_verification in app/api/auth.py.
    message: str = "If an account exists for this email and needs verification, a new link has been generated."
    verification_link: str
