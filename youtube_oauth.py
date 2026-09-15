from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet


YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)


def required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured.")
    return value


def callback_url() -> str:
    return (
        required_environment("PUBLIC_BASE_URL").rstrip("/")
        + "/recording/youtube/callback"
    )


def create_oauth_state(guild_id: int, user_id: int, action: str) -> str:
    if action not in {"enable", "disable"}:
        raise ValueError("Unsupported YouTube authorization action.")
    payload = {
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "action": action,
        "expires_at": int(time.time()) + 600,
        "nonce": secrets.token_urlsafe(16),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(
        required_environment("YOUTUBE_OAUTH_STATE_SECRET").encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def validate_oauth_state(state: str) -> dict[str, str]:
    try:
        encoded, supplied_signature = state.split(".", 1)
        expected_signature = hmac.new(
            required_environment("YOUTUBE_OAUTH_STATE_SECRET").encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid signature")
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if int(payload["expires_at"]) < int(time.time()):
            raise ValueError("expired state")
        return payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("The YouTube connection link is invalid or expired.") from error


def authorization_url_from_state(state: str) -> str:
    query = urlencode({
        "client_id": required_environment("GOOGLE_YOUTUBE_CLIENT_ID"),
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def connection_url(guild_id: int, user_id: int, action: str = "enable") -> str:
    state = create_oauth_state(guild_id, user_id, action)
    return (
        required_environment("PUBLIC_BASE_URL").rstrip("/")
        + "/recording/youtube/connect?"
        + urlencode({"state": state})
    )


def encrypt_refresh_token(refresh_token: str) -> str:
    key = required_environment("YOUTUBE_TOKEN_ENCRYPTION_KEY").encode("ascii")
    return Fernet(key).encrypt(refresh_token.encode("utf-8")).decode("ascii")


def decrypt_refresh_token(encrypted_refresh_token: str) -> str:
    key = required_environment("YOUTUBE_TOKEN_ENCRYPTION_KEY").encode("ascii")
    return Fernet(key).decrypt(encrypted_refresh_token.encode("ascii")).decode("utf-8")
