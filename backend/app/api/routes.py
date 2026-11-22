from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.agent.parser import make_parser
from app.auth.deps import get_principal
from app.auth.security import issue_token, verify_password
from app.config import get_settings
from app.db.engine import tenant_tx, unscoped_tx
from app.db.tables import approval_requests, audit_log, users
from app.orchestrator import ConfirmError, Orchestrator
from app.policy.principal import Principal
from app.responses import AgentResponse

router = APIRouter(prefix="/api")


@lru_cache
def get_orchestrator() -> Orchestrator:
    return Orchestrator(make_parser())


# ------------------------------------------------------------------ auth

class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn) -> dict:
    with unscoped_tx() as conn:
        row = conn.execute(text("SELECT * FROM auth_lookup(:e)"), {"e": body.email.strip()}).mappings().first()
    if row is None or not row["is_active"] or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    return {"token": issue_token(row["user_id"], row["hospital_id"])}


@router.get("/auth/demo-users")
def demo_users() -> list[dict]:
    if not get_settings().demo_mode:
        raise HTTPException(404, "Not found")
    with unscoped_tx() as conn:
        rows = conn.execute(text("SELECT * FROM demo_directory()")).mappings().all()
    return [dict(r) for r in rows]


@router.get("/me")
def me(p: Principal = Depends(get_principal)) -> dict:
    return {
        "user_id": p.user_id, "name": p.name, "email": p.email, "roles": list(p.roles),
        "hospital": {"id": p.hospital_id, "name": p.hospital_name, "city": p.hospital_city, "code": p.hospital_code},
        "permissions": {k: dict(v) for k, v in sorted(p.permissions.items())},
    }


# ------------------------------------------------------------------ chat

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None


@router.post("/chat", response_model=AgentResponse)
def chat(body: ChatIn, p: Principal = Depends(get_principal), orch: Orchestrator = Depends(get_orchestrator)):
    return orch.handle(p, body.message, body.conversation_id)


class ConfirmIn(BaseModel):
    typed_count: int | None = None


@router.post("/operations/{operation_id}/confirm", response_model=AgentResponse)
def confirm(operation_id: UUID, body: ConfirmIn, p: Principal = Depends(get_principal),
            orch: Orchestrator = Depends(get_orchestrator)):
    try:
        return orch.confirm(p, operation_id, body.typed_count)
    except ConfirmError as e:
        raise HTTPException(e.status, str(e))


@router.post("/operations/{operation_id}/cancel", response_model=AgentResponse)
def cancel(operation_id: UUID, p: Principal = Depends(get_principal), orch: Orchestrator = Depends(get_orchestrator)):
    try:
        return orch.cancel(p, operation_id)
    except ConfirmError as e:
        raise HTTPException(e.status, str(e))


# ------------------------------------------------------------------ approvals

class ApprovalIn(BaseModel):
    audit_id: int


@router.post("/approvals", response_model=AgentResponse)
def request_approval(body: ApprovalIn, p: Principal = Depends(get_principal),
                     orch: Orchestrator = Depends(get_orchestrator)):
    try:
        return orch.request_approval(p, body.audit_id)
    except ConfirmError as e:
        raise HTTPException(e.status, str(e))


@router.get("/approvals")
def list_approvals(p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    t = approval_requests
    q = select(t.c.id, t.c.summary, t.c.status, t.c.created_at, t.c.decided_at,
               users.c.name.label("requested_by")) \
        .select_from(t.join(users, users.c.id == t.c.requested_by)).where(t.c.hospital_id == p.hospital_id)
    if not p.has("users.manage_access"):
        q = q.where(t.c.requested_by == p.user_id)
    with tenant_tx(p.hospital_id) as conn:
        return [dict(r) for r in conn.execute(q.order_by(t.c.id.desc()).limit(50)).mappings().all()]


# ------------------------------------------------------------------ audit

@router.get("/audit")
def list_audit(limit: int = 50, p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    """Admins (audit.read) see their hospital's log; everyone else sees their own entries."""
    t = audit_log
    q = select(t.c.id, t.c.created_at, t.c.request_id, t.c.action, t.c.decision, t.c.reason, t.c.reason_code,
               t.c.rows_affected, t.c.raw_query, t.c.parsed_intent, users.c.name.label("user_name")) \
        .select_from(t.join(users, users.c.id == t.c.user_id)).where(t.c.hospital_id == p.hospital_id)
    if not p.has("audit.read"):
        q = q.where(t.c.user_id == p.user_id)
    with tenant_tx(p.hospital_id) as conn:
        return [dict(r) for r in conn.execute(q.order_by(t.c.id.desc()).limit(min(limit, 200))).mappings().all()]


@router.get("/health")
def health() -> dict:
    s = get_settings()
    is_qwen = s.llm_provider.lower() == "qwen"     # local model, no API key
    model = s.qwen_model if is_qwen else s.groq_model
    return {"ok": True, "llm_configured": is_qwen or bool(s.groq_api_key), "llm_provider": s.llm_provider, "llm_model": model,
            "time": datetime.now(tz=timezone.utc).isoformat()}
