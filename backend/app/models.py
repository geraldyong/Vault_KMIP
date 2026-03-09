from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateGroupRequest(BaseModel):
    group_name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    algorithm: Literal["AES", "HMAC_SHA256", "HMAC_SHA384", "HMAC_SHA512"] = "AES"
    key_length: int = Field(default=256, ge=64, le=4096)


class ApiMessage(BaseModel):
    ok: bool
    message: str
    data: dict[str, Any] | None = None


class RekeyRequest(BaseModel):
    activation_offset_seconds: int = Field(default=0, ge=0, le=86400)
