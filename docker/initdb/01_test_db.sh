#!/bin/bash
# Creates the test database alongside the main one on first start.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE DATABASE argusvue_test OWNER $POSTGRES_USER;
SQL
