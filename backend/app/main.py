from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .kmip_service import KmipDemoService
from .models import ApiMessage, CreateGroupRequest, RekeyRequest
from .state import STATE

service = KmipDemoService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    service.bootstrap()
    yield


app = FastAPI(title="Vault KMIP Demo API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", response_model=ApiMessage)
def health() -> ApiMessage:
    return ApiMessage(ok=True, message="healthy", data=service.health())


@app.get("/api/state", response_model=ApiMessage)
def state() -> ApiMessage:
    return ApiMessage(
        ok=True,
        message="current demo state",
        data={
            "memory": STATE.snapshot(),
            "vault": service.vault_objects_view(),
        },
    )


@app.post("/api/groups/create", response_model=ApiMessage)
def create_group(payload: CreateGroupRequest) -> ApiMessage:
    group = service.create_group(payload.group_name, payload.algorithm, payload.key_length)
    return ApiMessage(ok=True, message=f"created group '{payload.group_name}'", data=group)


@app.delete("/api/groups/{group_name}/delete", response_model=ApiMessage)
def delete_group(group_name: str) -> ApiMessage:
    result = service.delete_group(group_name)
    return ApiMessage(ok=True, message=f"deleted group '{group_name}'", data=result)


@app.post("/api/groups/{group_name}/rekey", response_model=ApiMessage)
def rekey_group(group_name: str, payload: RekeyRequest) -> ApiMessage:
    group = service.rekey_group(group_name, payload.activation_offset_seconds)
    return ApiMessage(ok=True, message=f"rekeyed KEK for '{group_name}'", data=group)

@app.get("/api/vault-browser")
def get_vault_browser() -> dict:
    return service.vault_browser_tree()
