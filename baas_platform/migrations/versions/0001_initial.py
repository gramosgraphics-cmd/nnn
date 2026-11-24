"""Esquema inicial: schemas platform/auth, tablas del plano de control y funciones.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS platform",
    "CREATE SCHEMA IF NOT EXISTS auth",
    "REVOKE ALL ON SCHEMA platform FROM PUBLIC",
    # ---- configuración interna (canal de realtime secreto, no legible por los tenants)
    """CREATE TABLE platform.config (key text PRIMARY KEY, value text NOT NULL)""",
    """INSERT INTO platform.config (key, value)
       VALUES ('realtime_channel', 'baas_' || replace(gen_random_uuid()::text, '-', ''))""",
    # ---- administradores y proyectos
    """CREATE TABLE platform.admins (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        email text UNIQUE NOT NULL,
        password_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE platform.projects (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        ref text UNIQUE NOT NULL,
        name text NOT NULL,
        schema_name text UNIQUE NOT NULL,
        jwt_secret text NOT NULL,
        anon_key text NOT NULL,
        service_role_key text NOT NULL,
        status text NOT NULL DEFAULT 'active',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    # ---- storage
    """CREATE TABLE platform.buckets (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id uuid NOT NULL REFERENCES platform.projects(id) ON DELETE CASCADE,
        name text NOT NULL,
        public boolean NOT NULL DEFAULT false,
        file_size_limit bigint,
        allowed_mime_types text[],
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (project_id, name))""",
    """CREATE TABLE platform.objects (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        bucket_id uuid NOT NULL REFERENCES platform.buckets(id) ON DELETE CASCADE,
        name text NOT NULL,
        owner uuid,
        size bigint NOT NULL DEFAULT 0,
        mime_type text,
        metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (bucket_id, name))""",
    # ---- functions y webhooks
    """CREATE TABLE platform.functions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id uuid NOT NULL REFERENCES platform.projects(id) ON DELETE CASCADE,
        name text NOT NULL,
        kind text NOT NULL DEFAULT 'python',
        code text,
        url text,
        timeout_seconds int NOT NULL DEFAULT 10,
        env jsonb NOT NULL DEFAULT '{}'::jsonb,
        verify_jwt boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (project_id, name))""",
    """CREATE TABLE platform.function_logs (
        id bigserial PRIMARY KEY,
        function_id uuid NOT NULL REFERENCES platform.functions(id) ON DELETE CASCADE,
        project_id uuid NOT NULL,
        status text NOT NULL,
        duration_ms int NOT NULL DEFAULT 0,
        request jsonb,
        response jsonb,
        logs text,
        created_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX function_logs_fn_idx ON platform.function_logs (function_id, created_at DESC)",
    """CREATE TABLE platform.webhooks (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id uuid NOT NULL REFERENCES platform.projects(id) ON DELETE CASCADE,
        url text NOT NULL,
        events text[] NOT NULL DEFAULT ARRAY['*'],
        secret text NOT NULL,
        enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE platform.webhook_deliveries (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        webhook_id uuid NOT NULL REFERENCES platform.webhooks(id) ON DELETE CASCADE,
        event jsonb NOT NULL,
        status text NOT NULL DEFAULT 'pending',
        attempts int NOT NULL DEFAULT 0,
        next_attempt_at timestamptz NOT NULL DEFAULT now(),
        last_error text,
        response_status int,
        created_at timestamptz NOT NULL DEFAULT now(),
        delivered_at timestamptz)""",
    "CREATE INDEX webhook_deliveries_pending_idx ON platform.webhook_deliveries (next_attempt_at) WHERE status = 'pending'",
    # ---- backups y logs de peticiones
    """CREATE TABLE platform.backups (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        project_ref text NOT NULL,
        file_path text,
        size_bytes bigint,
        status text NOT NULL DEFAULT 'running',
        error text,
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz)""",
    """CREATE TABLE platform.request_logs (
        id bigserial PRIMARY KEY,
        project_id uuid NOT NULL,
        ts timestamptz NOT NULL DEFAULT now(),
        service text NOT NULL,
        method text NOT NULL,
        path text NOT NULL,
        status int NOT NULL,
        duration_ms int NOT NULL DEFAULT 0)""",
    "CREATE INDEX request_logs_project_ts_idx ON platform.request_logs (project_id, ts DESC)",
    # ---- funciones auxiliares para políticas RLS (auth.uid(), auth.role(), auth.jwt())
    """CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
       $$ SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$""",
    """CREATE OR REPLACE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS
       $$ SELECT nullif(current_setting('request.jwt.claim.role', true), '') $$""",
    """CREATE OR REPLACE FUNCTION auth.email() RETURNS text LANGUAGE sql STABLE AS
       $$ SELECT nullif(current_setting('request.jwt.claim.email', true), '') $$""",
    """CREATE OR REPLACE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS
       $$ SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $$""",
    "GRANT USAGE ON SCHEMA auth TO PUBLIC",
    # ---- trigger genérico de realtime (SECURITY DEFINER, canal secreto)
    """CREATE OR REPLACE FUNCTION platform.notify_change() RETURNS trigger
       LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
       DECLARE
         payload text;
         v_new jsonb;
         v_old jsonb;
         chan text;
       BEGIN
         IF TG_OP IN ('INSERT', 'UPDATE') THEN v_new := to_jsonb(NEW); END IF;
         IF TG_OP IN ('UPDATE', 'DELETE') THEN v_old := to_jsonb(OLD); END IF;
         payload := jsonb_build_object(
           'schema', TG_TABLE_SCHEMA, 'table', TG_TABLE_NAME, 'type', TG_OP,
           'record', v_new, 'old_record', v_old, 'commit_timestamp', now())::text;
         IF length(payload) > 7500 THEN
           payload := jsonb_build_object(
             'schema', TG_TABLE_SCHEMA, 'table', TG_TABLE_NAME, 'type', TG_OP,
             'record', null, 'old_record', null, 'truncated', true,
             'commit_timestamp', now())::text;
         END IF;
         SELECT value INTO chan FROM platform.config WHERE key = 'realtime_channel';
         PERFORM pg_notify(chan, payload);
         RETURN NULL;
       END $$""",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS platform CASCADE")
    op.execute("DROP SCHEMA IF EXISTS auth CASCADE")
