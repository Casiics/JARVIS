-- Signed checkpoints of the audit chain (core/jarvis_core/audit/checkpoint.py).
-- Append-only like audit.log. The database checks that each checkpoint signs the real head at
-- its position and that checkpoints never go backwards; the signature itself is verified
-- offline with the public key (python -m jarvis_core.audit verify).
-- Existing installations apply this file once:
--   docker compose exec -T pg-audit psql -U postgres -d postgres < docker/postgres/audit/sql/checkpoints.sql

CREATE TABLE audit.checkpoint (
    id          bigserial   PRIMARY KEY,
    body        text        NOT NULL,
    upto_seq    bigint      NOT NULL CHECK (upto_seq >= 0),
    head_hash   char(64)    NOT NULL,
    signed_at   timestamptz NOT NULL,
    inserted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER audit_checkpoint_no_update_delete
    BEFORE UPDATE OR DELETE ON audit.checkpoint
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_change();

CREATE TRIGGER audit_checkpoint_no_truncate
    BEFORE TRUNCATE ON audit.checkpoint
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_change();

-- Number of entries and the newest entry hash, read in one statement.
CREATE FUNCTION audit.tip(OUT n bigint, OUT head text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, audit AS $$
    SELECT (SELECT count(*) FROM audit.log), audit.head();
$$;

CREATE FUNCTION audit.last_checkpoint() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, audit AS $$
    SELECT body FROM audit.checkpoint ORDER BY id DESC LIMIT 1;
$$;

CREATE FUNCTION audit.checkpoint_append(p_body text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, audit AS $$
DECLARE
    v_body jsonb := p_body::jsonb;
    v_upto bigint := (v_body ->> 'upto_seq')::bigint;
    v_head text := v_body ->> 'head_hash';
    v_signed timestamptz := (v_body ->> 'signed_at')::timestamptz;
    v_actual text;
    v_last audit.checkpoint%ROWTYPE;
    v_id bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('audit.log'));
    IF v_upto IS NULL OR v_upto < 0 OR v_upto > (SELECT count(*) FROM audit.log) THEN
        RAISE EXCEPTION 'checkpoint position is outside the log';
    END IF;
    IF v_upto = 0 THEN
        v_actual := repeat('0', 64);
    ELSE
        SELECT entry_hash INTO v_actual FROM audit.log ORDER BY seq OFFSET v_upto - 1 LIMIT 1;
    END IF;
    IF v_head IS DISTINCT FROM v_actual THEN
        RAISE EXCEPTION 'checkpoint head does not match the log';
    END IF;
    SELECT * INTO v_last FROM audit.checkpoint ORDER BY id DESC LIMIT 1;
    IF FOUND AND (v_upto < v_last.upto_seq OR v_signed <= v_last.signed_at) THEN
        RAISE EXCEPTION 'checkpoints must not go backwards';
    END IF;
    INSERT INTO audit.checkpoint (body, upto_seq, head_hash, signed_at)
    VALUES (p_body, v_upto, v_head, v_signed)
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

REVOKE ALL ON audit.checkpoint FROM PUBLIC;
REVOKE ALL ON SEQUENCE audit.checkpoint_id_seq FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION audit.tip(), audit.last_checkpoint(), audit.checkpoint_append(text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit.tip() TO audit_writer, audit_reader;
GRANT EXECUTE ON FUNCTION audit.last_checkpoint() TO audit_writer, audit_reader;
GRANT EXECUTE ON FUNCTION audit.checkpoint_append(text) TO audit_writer;
GRANT SELECT ON audit.checkpoint TO audit_reader;
