#!/usr/bin/env bash
# Runs once, on the first start of an empty PostgreSQL volume (docker-entrypoint-initdb.d).
# Airflow's metadata and AutoAir's state get separate roles and databases, as in production.
# Changing DEMO_*_DB_PASSWORD later does not rotate these passwords: `down -v` or ALTER ROLE.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v airflow_pw="$DEMO_AIRFLOW_DB_PASSWORD" -v autoair_pw="$DEMO_AUTOAIR_DB_PASSWORD" <<'SQL'
CREATE ROLE airflow LOGIN PASSWORD :'airflow_pw';
CREATE DATABASE airflow OWNER airflow;
CREATE ROLE autoair LOGIN PASSWORD :'autoair_pw';
CREATE DATABASE autoair OWNER autoair;
REVOKE ALL ON DATABASE airflow FROM PUBLIC;
REVOKE ALL ON DATABASE autoair FROM PUBLIC;
SQL
