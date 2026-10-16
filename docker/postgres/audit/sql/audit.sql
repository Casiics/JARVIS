-- Append-only, hash-chained audit log (docs/architecture.md, section 8).
-- The writer role can only call audit.head() and audit.append(); it has no table privileges.
-- Triggers block UPDATE, DELETE and TRUNCATE for every role, including the table owner.

DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
END;
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA audit;
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

CREATE TABLE audit.log (
    seq          bigserial PRIMARY KEY,
    payload      text        NOT NULL,
    payload_json jsonb       NOT NULL,
    prev_hash    char(64)    NOT NULL,
    entry_hash   char(64)    NOT NULL UNIQUE,
    inserted_at  timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION audit.forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.log is append-only';
END;
$$;

CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit.log
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_change();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit.log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_change();

CREATE FUNCTION audit.head() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, audit AS $$
    SELECT coalesce(
        (SELECT entry_hash::text FROM audit.log ORDER BY seq DESC LIMIT 1),
        repeat('0', 64)
    );
$$;

-- Verifies the chain link in the database, so a writer cannot insert a forged hash.
CREATE FUNCTION audit.append(p_payload text, p_prev_hash text, p_entry_hash text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, audit AS $$
DECLARE
    v_seq bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('audit.log'));
    IF p_prev_hash <> audit.head() THEN
        RAISE EXCEPTION 'prev_hash does not match current head';
    END IF;
    IF p_entry_hash <> encode(sha256(convert_to(p_prev_hash || p_payload, 'UTF8')), 'hex') THEN
        RAISE EXCEPTION 'entry_hash does not match payload';
    END IF;
    INSERT INTO audit.log (payload, payload_json, prev_hash, entry_hash)
    VALUES (p_payload, p_payload::jsonb, p_prev_hash, p_entry_hash)
    RETURNING seq INTO v_seq;
    RETURN v_seq;
END;
$$;

REVOKE ALL ON ALL TABLES IN SCHEMA audit FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA audit FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA audit FROM PUBLIC;

CREATE ROLE audit_writer NOLOGIN;
CREATE ROLE audit_reader NOLOGIN;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO audit_writer, audit_reader', current_database());
END;
$$;
GRANT USAGE ON SCHEMA audit TO audit_writer, audit_reader;
GRANT EXECUTE ON FUNCTION audit.head() TO audit_writer, audit_reader;
GRANT EXECUTE ON FUNCTION audit.append(text, text, text) TO audit_writer;
GRANT SELECT ON audit.log TO audit_reader;
