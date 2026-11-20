-- Core schema. Every business table carries hospital_id (tenant key).

CREATE TABLE IF NOT EXISTS hospitals (
    id          SERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- short code, e.g. 'A'
    name        TEXT NOT NULL UNIQUE,
    city        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    hospital_id     INT  NOT NULL REFERENCES hospitals(id),
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_hospital_idx ON users(hospital_id);

CREATE TABLE IF NOT EXISTS roles (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,      -- machine name, e.g. billing_manager
    display_name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    id              SERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,      -- e.g. inventory.update
    description     TEXT NOT NULL
);

-- constraints: {"max_rows": 500} | {"max_discount_pct": 10} | {"own_records_only": true}
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id         INT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id   INT NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    constraints     JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id         INT NOT NULL REFERENCES roles(id),
    hospital_id     INT NOT NULL REFERENCES hospitals(id),
    granted_by      INT REFERENCES users(id),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role_id)
);

-- Per-user exceptions on top of roles. 'deny' removes a permission a role grants
-- (e.g. "remove inventory edit access from Ravi" while keeping his other pharmacist rights).
CREATE TABLE IF NOT EXISTS user_permission_overrides (
    id              SERIAL PRIMARY KEY,
    hospital_id     INT NOT NULL REFERENCES hospitals(id),
    user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_id   INT NOT NULL REFERENCES permissions(id),
    effect          TEXT NOT NULL CHECK (effect IN ('grant', 'deny')),
    constraints     JSONB NOT NULL DEFAULT '{}'::jsonb,
    granted_by      INT REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, permission_id)
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id              SERIAL PRIMARY KEY,
    hospital_id     INT NOT NULL REFERENCES hospitals(id),
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    batch_no        TEXT NOT NULL,
    expiry_date     DATE NOT NULL,
    quantity        INT NOT NULL CHECK (quantity >= 0),
    unit_price      NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0),
    reorder_level   INT NOT NULL DEFAULT 20,
    version         INT NOT NULL DEFAULT 1,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inventory_hospital_idx ON inventory_items(hospital_id);

CREATE TABLE IF NOT EXISTS bills (
    id              SERIAL PRIMARY KEY,
    hospital_id     INT NOT NULL REFERENCES hospitals(id),
    patient_name    TEXT NOT NULL,
    bill_date       DATE NOT NULL,             -- business date in IST
    gross_amount    NUMERIC(12, 2) NOT NULL CHECK (gross_amount >= 0),
    discount_pct    NUMERIC(5, 2) NOT NULL DEFAULT 0 CHECK (discount_pct >= 0 AND discount_pct <= 100),
    net_amount      NUMERIC(12, 2) NOT NULL CHECK (net_amount >= 0),
    status          TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'cancelled', 'refunded')),
    created_by      INT REFERENCES users(id),  -- enables "own bills only"
    version         INT NOT NULL DEFAULT 1,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bills_hospital_date_idx ON bills(hospital_id, bill_date);

CREATE TABLE IF NOT EXISTS audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    hospital_id         INT NOT NULL REFERENCES hospitals(id),
    user_id             INT NOT NULL REFERENCES users(id),
    request_id          UUID NOT NULL,         -- links preview -> executed/cancelled rows
    operation_id        UUID,
    raw_query           TEXT NOT NULL,
    parsed_intent       JSONB,                 -- what the LLM proposed
    resolved_operation  JSONB,                 -- what code resolved it to (dates, ids, filters)
    action              TEXT,
    decision            TEXT NOT NULL CHECK (decision IN
                          ('answered', 'clarified', 'refused', 'previewed', 'executed',
                           'cancelled', 'expired', 'failed')),
    reason              TEXT,
    reason_code         TEXT,
    rows_affected       INT NOT NULL DEFAULT 0,
    before_snapshot     JSONB,
    after_snapshot      JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_hospital_created_idx ON audit_log(hospital_id, created_at DESC);

-- A previewed write waiting for confirmation.
CREATE TABLE IF NOT EXISTS pending_operations (
    id                  UUID PRIMARY KEY,
    hospital_id         INT NOT NULL REFERENCES hospitals(id),
    user_id             INT NOT NULL REFERENCES users(id),
    request_id          UUID NOT NULL,
    raw_query           TEXT NOT NULL,
    action              TEXT NOT NULL,
    operation           JSONB NOT NULL,
    parsed_intent       JSONB,
    target_ids          JSONB NOT NULL,
    expected_count      INT NOT NULL,
    requires_typed_count BOOLEAN NOT NULL,
    preview             JSONB NOT NULL,
    approval_request_id INT,
    status              TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'executed', 'cancelled', 'expired', 'failed')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NOT NULL,
    resolved_at         TIMESTAMPTZ
);

-- Requests above a user's ceiling, routed to an admin.
CREATE TABLE IF NOT EXISTS approval_requests (
    id                  SERIAL PRIMARY KEY,
    hospital_id         INT NOT NULL REFERENCES hospitals(id),
    requested_by        INT NOT NULL REFERENCES users(id),
    raw_query           TEXT NOT NULL,
    action              TEXT NOT NULL,
    operation           JSONB NOT NULL,
    summary             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by          INT REFERENCES users(id),
    decided_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Minimal conversation memory so clarifying questions can be answered.
-- Only user text and the agent's own questions are stored: never DB rows.
CREATE TABLE IF NOT EXISTS conversation_turns (
    id                  BIGSERIAL PRIMARY KEY,
    hospital_id         INT NOT NULL REFERENCES hospitals(id),
    user_id             INT NOT NULL REFERENCES users(id),
    conversation_id     UUID NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content             TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_idx ON conversation_turns(conversation_id, id);
