-- Per-domain database (one Postgres instance per domain).
-- app:      the domain's deterministic services (n8n executor, risk gate, memory service).
-- agent_ro: agents' MCP server; SELECT on curated views in schema agent_views only.

DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
END;
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE app NOLOGIN;
CREATE ROLE agent_ro NOLOGIN;

CREATE SCHEMA app AUTHORIZATION app;
CREATE SCHEMA agent_views AUTHORIZATION app;
REVOKE ALL ON SCHEMA app, agent_views FROM PUBLIC;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app, agent_ro', current_database());
END;
$$;
GRANT USAGE ON SCHEMA agent_views TO agent_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE app IN SCHEMA agent_views GRANT SELECT ON TABLES TO agent_ro;
