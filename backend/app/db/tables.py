"""SQLAlchemy Core table definitions (mirror of migrations/001_schema.sql).

Every dynamic query in the app is built from these objects with allowlisted columns;
no SQL text ever comes from the LLM.
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, Date, Integer, MetaData, Numeric, Table, Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

metadata = MetaData()

hospitals = Table(
    "hospitals", metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text), Column("name", Text), Column("city", Text),
)

users = Table(
    "users", metadata,
    Column("id", Integer, primary_key=True),
    Column("hospital_id", Integer), Column("name", Text), Column("email", Text),
    Column("password_hash", Text), Column("is_active", Boolean),
    Column("created_at", TIMESTAMP(timezone=True)),
)

roles = Table(
    "roles", metadata,
    Column("id", Integer, primary_key=True), Column("name", Text), Column("display_name", Text),
)

permissions = Table(
    "permissions", metadata,
    Column("id", Integer, primary_key=True), Column("code", Text), Column("description", Text),
)

role_permissions = Table(
    "role_permissions", metadata,
    Column("role_id", Integer, primary_key=True),
    Column("permission_id", Integer, primary_key=True),
    Column("constraints", JSONB),
)

user_roles = Table(
    "user_roles", metadata,
    Column("user_id", Integer, primary_key=True), Column("role_id", Integer, primary_key=True),
    Column("hospital_id", Integer), Column("granted_by", Integer),
    Column("granted_at", TIMESTAMP(timezone=True)),
)

user_permission_overrides = Table(
    "user_permission_overrides", metadata,
    Column("id", Integer, primary_key=True),
    Column("hospital_id", Integer), Column("user_id", Integer), Column("permission_id", Integer),
    Column("effect", Text), Column("constraints", JSONB), Column("granted_by", Integer),
    Column("created_at", TIMESTAMP(timezone=True)),
)

inventory_items = Table(
    "inventory_items", metadata,
    Column("id", Integer, primary_key=True),
    Column("hospital_id", Integer), Column("name", Text), Column("category", Text),
    Column("batch_no", Text), Column("expiry_date", Date), Column("quantity", Integer),
    Column("unit_price", Numeric(12, 2)), Column("reorder_level", Integer),
    Column("version", Integer), Column("updated_at", TIMESTAMP(timezone=True)),
)

bills = Table(
    "bills", metadata,
    Column("id", Integer, primary_key=True),
    Column("hospital_id", Integer), Column("patient_name", Text), Column("bill_date", Date),
    Column("gross_amount", Numeric(12, 2)), Column("discount_pct", Numeric(5, 2)),
    Column("net_amount", Numeric(12, 2)), Column("status", Text), Column("created_by", Integer),
    Column("version", Integer), Column("updated_at", TIMESTAMP(timezone=True)),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("hospital_id", Integer), Column("user_id", Integer),
    Column("request_id", UUID(as_uuid=True)), Column("operation_id", UUID(as_uuid=True)),
    Column("raw_query", Text), Column("parsed_intent", JSONB), Column("resolved_operation", JSONB),
    Column("action", Text), Column("decision", Text), Column("reason", Text),
    Column("reason_code", Text), Column("rows_affected", Integer),
    Column("before_snapshot", JSONB), Column("after_snapshot", JSONB),
    Column("created_at", TIMESTAMP(timezone=True)),
)

pending_operations = Table(
    "pending_operations", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("hospital_id", Integer), Column("user_id", Integer),
    Column("request_id", UUID(as_uuid=True)), Column("raw_query", Text), Column("action", Text),
    Column("operation", JSONB), Column("parsed_intent", JSONB), Column("target_ids", JSONB),
    Column("expected_count", Integer), Column("requires_typed_count", Boolean),
    Column("preview", JSONB), Column("approval_request_id", Integer), Column("status", Text),
    Column("created_at", TIMESTAMP(timezone=True)), Column("expires_at", TIMESTAMP(timezone=True)),
    Column("resolved_at", TIMESTAMP(timezone=True)),
)

approval_requests = Table(
    "approval_requests", metadata,
    Column("id", Integer, primary_key=True),
    Column("hospital_id", Integer), Column("requested_by", Integer), Column("raw_query", Text),
    Column("action", Text), Column("operation", JSONB), Column("summary", Text),
    Column("status", Text), Column("decided_by", Integer),
    Column("decided_at", TIMESTAMP(timezone=True)), Column("created_at", TIMESTAMP(timezone=True)),
)

conversation_turns = Table(
    "conversation_turns", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("hospital_id", Integer), Column("user_id", Integer),
    Column("conversation_id", UUID(as_uuid=True)), Column("role", Text), Column("content", Text),
    Column("created_at", TIMESTAMP(timezone=True)),
)
