import time,traceback
from pathlib import Path
from src.config.database import Session,Base,engine
from src.config.schema import apply_runtime_migrations
from src.config.settings import JOB_QUEUE_KEY,WORKER_POLL_SECONDS
from src.services import jobs

Base.metadata.create_all(engine)
apply_runtime_migrations()

WORKER_ID=jobs.worker_identity()
RECOVERY_INTERVAL=5.0


def recover_once():
    with Session() as db:
        try:
            ids=jobs.recover(db)
            jobs.repair_completed(db)
        except Exception:
            traceback.print_exc();return
    for review_id in ids:jobs.enqueue(review_id)


def main():
    last_recovery=0.0
    Path('/tmp/worker-heartbeat').touch()
    while True:
        Path('/tmp/worker-heartbeat').touch()
        now=time.monotonic()
        if now-last_recovery>=RECOVERY_INTERVAL:
            recover_once();last_recovery=now
        try:
            item=jobs.redis_client().blpop(JOB_QUEUE_KEY,timeout=max(1,int(WORKER_POLL_SECONDS)))
        except Exception:
            # Redis outage: keep serving recovery scans so accepted reviews resume.
            traceback.print_exc();time.sleep(2);continue
        if not item:continue
        try:
            jobs.run_attempt(int(item[1]),WORKER_ID)
        except Exception:
            traceback.print_exc()


if __name__=='__main__':
    main()
