from sqlalchemy import text
from src.config.database import engine

# create_all owns new tables. Existing demo volumes still need the job-lifecycle
# columns added in place, because there is no external migration runner.
_COLUMNS=[
    ('attempt_count','INTEGER NOT NULL DEFAULT 0'),
    ('max_attempts','INTEGER NOT NULL DEFAULT 3'),
    ('execution_token',"VARCHAR(64) NOT NULL DEFAULT ''"),
    ('lease_owner',"VARCHAR(128) NOT NULL DEFAULT ''"),
    ('lease_expires_at','TIMESTAMPTZ'),
    ('next_attempt_at','TIMESTAMPTZ'),
    ('completed_at','TIMESTAMPTZ'),
    ('approved_at','TIMESTAMPTZ'),
    ('failure_reason',"TEXT NOT NULL DEFAULT ''"),
    ('snapshot_checksum',"VARCHAR(64) NOT NULL DEFAULT ''"),
    ('artifact_checksum',"VARCHAR(64) NOT NULL DEFAULT ''"),
]

def apply_runtime_migrations():
    with engine.begin() as conn:
        for name,ddl in _COLUMNS:
            conn.execute(text(f'ALTER TABLE reviews ADD COLUMN IF NOT EXISTS {name} {ddl}'))
