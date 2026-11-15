import os
DATABASE_URL=os.getenv("DATABASE_URL", "postgresql+psycopg://mill:mill@db/mill")
REDIS_URL=os.getenv("REDIS_URL", "redis://redis:6379/0")
JWT_SECRET=os.getenv("JWT_SECRET", "local-mill-change-this-secret-at-least-32")
# Shared artifacts volume. The worker writes here; the API verifies the same file before release.
ARTIFACTS_DIR=os.getenv("ARTIFACTS_DIR", "/artifacts")
JOB_QUEUE_KEY=os.getenv("JOB_QUEUE_KEY", "mill:jobs")
# A claim is valid only until this lease expires. A restarted worker may take over an expired claim.
JOB_LEASE_SECONDS=int(os.getenv("JOB_LEASE_SECONDS", "30"))
# Total execution attempts, including the first one. Retryable failures are capped by this value.
JOB_MAX_ATTEMPTS=int(os.getenv("JOB_MAX_ATTEMPTS", "3"))
JOB_RETRY_BASE_SECONDS=float(os.getenv("JOB_RETRY_BASE_SECONDS", "1"))
JOB_RETRY_MAX_SECONDS=float(os.getenv("JOB_RETRY_MAX_SECONDS", "10"))
WORKER_POLL_SECONDS=float(os.getenv("WORKER_POLL_SECONDS", "0.5"))
