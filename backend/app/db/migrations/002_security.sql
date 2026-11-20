-- Defence in depth for tenant isolation and audit integrity.
-- The API connects as hms_app (created by migrate.py), which does NOT own any table,
-- so row-level security always applies to it. Migrations and the seed run as the owner.

-- 1. Row-level security: every tenant table is filtered by the per-transaction setting
--    app.hospital_id. If the setting is missing, current_setting(..., true) is NULL and
--    the policy matches no rows (fail closed).
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users', 'user_roles', 'user_permission_overrides', 'inventory_items', 'bills',
    'audit_log', 'pending_operations', 'approval_requests', 'conversation_turns'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I
         USING (hospital_id = NULLIF(current_setting(''app.hospital_id'', true), '''')::int)
         WITH CHECK (hospital_id = NULLIF(current_setting(''app.hospital_id'', true), '''')::int)',
      t);
  END LOOP;
END $$;

-- 2. Least-privilege grants for the application role.
GRANT USAGE ON SCHEMA public TO hms_app;
GRANT SELECT ON hospitals, roles, permissions, role_permissions TO hms_app;
GRANT SELECT ON users TO hms_app;
GRANT SELECT, INSERT, DELETE ON user_roles TO hms_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_permission_overrides TO hms_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON inventory_items TO hms_app;
GRANT SELECT, UPDATE ON bills TO hms_app;                 -- no DELETE on bills at the DB level
GRANT SELECT, INSERT ON audit_log TO hms_app;              -- append-only for the app
GRANT SELECT, INSERT, UPDATE ON pending_operations TO hms_app;
GRANT SELECT, INSERT, UPDATE ON approval_requests TO hms_app;
GRANT SELECT, INSERT ON conversation_turns TO hms_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hms_app;

-- 3. Audit log is append-only, even for roles that could otherwise UPDATE/DELETE it.
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- 4. Login needs to find a user before we know their hospital. This narrow
--    SECURITY DEFINER function is the only cross-tenant read the app can do.
CREATE OR REPLACE FUNCTION auth_lookup(p_email TEXT)
RETURNS TABLE (user_id INT, hospital_id INT, password_hash TEXT, is_active BOOLEAN)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT u.id, u.hospital_id, u.password_hash, u.is_active
  FROM users u WHERE lower(u.email) = lower(p_email)
$$;
REVOKE ALL ON FUNCTION auth_lookup(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_lookup(TEXT) TO hms_app;

-- Demo-only directory for the role switcher (exposed by the API only when DEMO_MODE=true).
CREATE OR REPLACE FUNCTION demo_directory()
RETURNS TABLE (user_id INT, name TEXT, email TEXT, hospital_id INT, hospital_name TEXT,
               hospital_code TEXT, roles TEXT[])
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT u.id, u.name, u.email, h.id, h.name, h.code,
         COALESCE(array_agg(r.name ORDER BY r.name) FILTER (WHERE r.name IS NOT NULL), '{}')
  FROM users u
  JOIN hospitals h ON h.id = u.hospital_id
  LEFT JOIN user_roles ur ON ur.user_id = u.id
  LEFT JOIN roles r ON r.id = ur.role_id
  WHERE u.is_active
  GROUP BY u.id, h.id
  ORDER BY h.id, u.id
$$;
REVOKE ALL ON FUNCTION demo_directory() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION demo_directory() TO hms_app;
