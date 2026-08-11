-- Base schema and extensions.
-- pgcrypto supplies digest() so content hashes can be computed set-wise in SQL
-- during bulk loads rather than row-by-row in Python.

CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS staging;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

COMMENT ON SCHEMA market IS
    'Source-of-record fact and dimension tables. Postgres is the only writer.';
COMMENT ON SCHEMA staging IS
    'Transient COPY targets for bulk loads. Contents are not durable.';
