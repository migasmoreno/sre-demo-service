"""
Database and service configuration for sre-demo-service.

⚠️  Known misconfiguration (incident #2026-05-04):
    DB_CONNECT_TIMEOUT = 0 causes immediate connection timeouts.
    DB_MAX_RETRIES = 0 means no retry on transient failures.
    DB_POOL_SIZE = 0 means the connection pool is empty — all connections fail.

    These values were introduced in commit abc1234 by mistake.
    Correct values are documented in ops/runbook.md.
"""

import os

# --- Service identity ---
SERVICE_NAME    = os.getenv("K_SERVICE",  "sre-demo-service")
SERVICE_VERSION = os.getenv("K_REVISION", "local")
ENVIRONMENT     = os.getenv("ENVIRONMENT", "production")

# --- Database connection ---
DB_HOST            = os.getenv("DB_HOST", "payments-db.internal")
DB_PORT            = int(os.getenv("DB_PORT", "5432"))
DB_NAME            = os.getenv("DB_NAME", "payments")
DB_USER            = os.getenv("DB_USER", "svc_payments")

# BUG: These values are wrong — they were incorrectly set to 0 in a bad deploy.
# Correct values: DB_CONNECT_TIMEOUT=30, DB_MAX_RETRIES=3, DB_POOL_SIZE=10
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "30"))   # should be 30
DB_MAX_RETRIES     = int(os.getenv("DB_MAX_RETRIES",     "3"))   # should be 3
DB_POOL_SIZE       = int(os.getenv("DB_POOL_SIZE",       "10"))   # should be 10

# --- Payment processor ---
PAYMENT_PROCESSOR_TIMEOUT_MS = int(os.getenv("PAYMENT_PROCESSOR_TIMEOUT_MS", "5000"))
