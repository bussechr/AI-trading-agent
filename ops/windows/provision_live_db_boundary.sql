\set ON_ERROR_STOP on

-- Run as a PostgreSQL administrator while connected to the fxstack database.
-- The runtime retains its existing login and database ownership, but loses
-- cluster-wide authority.  The research marker is deliberately NOLOGIN and
-- the runtime role is not granted membership in it.
ALTER ROLE fx WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fxstack_research') THEN
        CREATE ROLE fxstack_research WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;

REVOKE fxstack_research FROM fx;

SELECT
    rolname,
    rolsuper,
    rolcreaterole,
    rolcreatedb,
    rolcanlogin
FROM pg_roles
WHERE rolname IN ('fx', 'fxstack_research')
ORDER BY rolname;
