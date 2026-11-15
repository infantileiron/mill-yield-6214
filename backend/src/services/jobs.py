import socket, threading, uuid
from datetime import timedelta
from sqlalchemy import select, update, or_
from src.config.database import Session
from src.config.settings import (REDIS_URL,JOB_QUEUE_KEY,JOB_LEASE_SECONDS,JOB_MAX_ATTEMPTS,
                                 JOB_RETRY_BASE_SECONDS,JOB_RETRY_MAX_SECONDS)
from src.constants.enums import ReviewStatus,AttemptStatus,AttemptPhase
from src.models.entities import Review,ReviewAttempt
from src.services import artifacts
from src.services.statistics import diagnose
from src.utils.time import utcnow,aware

class PermanentError(RuntimeError):
    pass

# Redis is a wake-up hint only. A review is always claimed and fenced in PostgreSQL,
# so a lost, duplicated or replayed message cannot start a second writer.
_redis=None

def redis_client():
    global _redis
    if _redis is None:
        from redis import Redis
        _redis=Redis.from_url(REDIS_URL,decode_responses=True,socket_connect_timeout=1,socket_timeout=2)
    return _redis

def enqueue(review_id):
    """Best effort wake-up. Failure is safe: recovery scanning finds the review."""
    try:
        redis_client().rpush(JOB_QUEUE_KEY,int(review_id))
        return True
    except Exception:
        return False

def worker_identity():
    return f'{socket.gethostname()}:{uuid.uuid4().hex[:8]}'

def _lease_deadline():
    return utcnow()+timedelta(seconds=JOB_LEASE_SECONDS)

def _attempt_no(review):
    return max(0,int(review.attempt_count or 0))

def claim(db,review_id,worker_id):
    """Take exclusive execution rights for one attempt, or return None.

    SELECT ... FOR UPDATE SKIP LOCKED makes duplicate deliveries harmless; an expired
    lease is the only way another process may take over. Each claim rotates the
    execution token, so the previous owner's writes are fenced out from then on.
    """
    now=utcnow()
    row=db.execute(
        select(Review)
        .where(Review.id==int(review_id))
        .where(Review.status.in_([ReviewStatus.queued.value,ReviewStatus.running.value]))
        .where(or_(Review.lease_expires_at.is_(None),Review.lease_expires_at<now))
        .where(or_(Review.next_attempt_at.is_(None),Review.next_attempt_at<=now))
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if row is None:
        db.rollback();return None
    max_attempts=max(1,int(row.max_attempts or JOB_MAX_ATTEMPTS))
    # Close out an attempt whose process disappeared without releasing the lease.
    db.execute(update(ReviewAttempt)
               .where(ReviewAttempt.review_id==row.id,ReviewAttempt.status==AttemptStatus.running.value)
               .values(status=AttemptStatus.abandoned.value,phase=AttemptPhase.abandoned.value,
                       finished_at=now,error='执行进程中断，租约到期后由新进程接管'))
    if _attempt_no(row)>=max_attempts:
        reason=row.failure_reason or '执行中断'
        row.status=ReviewStatus.failed.value
        row.failure_reason=f'已用完 {max_attempts} 次尝试：{reason}'
        row.execution_token='';row.lease_owner='';row.lease_expires_at=None;row.next_attempt_at=None
        db.commit();return None
    token=uuid.uuid4().hex
    attempt_no=_attempt_no(row)+1
    row.attempt_count=attempt_no
    row.execution_token=token
    row.lease_owner=worker_id
    row.lease_expires_at=_lease_deadline()
    row.next_attempt_at=None
    row.status=ReviewStatus.running.value
    row.failure_reason=''
    attempt=ReviewAttempt(review_id=row.id,attempt_no=attempt_no,worker_id=worker_id,
                          execution_token=token,status=AttemptStatus.running.value,
                          phase=AttemptPhase.claimed.value,heartbeat_at=now)
    db.add(attempt);db.commit()
    return {'review_id':row.id,'token':token,'attempt_no':attempt_no,'worker_id':worker_id,
            'max_attempts':max_attempts}

def _fenced(db,review_id,token,**values):
    """Apply an update only while the caller still holds the execution right."""
    result=db.execute(update(Review)
                      .where(Review.id==review_id,Review.execution_token==token,
                             Review.status==ReviewStatus.running.value)
                      .values(**values))
    return result.rowcount==1

def _attempt_update(db,token,**values):
    db.execute(update(ReviewAttempt).where(ReviewAttempt.execution_token==token).values(**values))

def set_phase(db,review_id,token,phase):
    if not _fenced(db,review_id,token,lease_expires_at=_lease_deadline()):
        db.rollback();return False
    _attempt_update(db,token,phase=phase,heartbeat_at=utcnow())
    db.commit();return True

class _LeaseHeartbeat:
    """Keep extending our lease while the attempt is running.

    Without this a slow computation would let the lease expire mid-flight and a
    second worker could legitimately take over; the heartbeat keeps the live owner
    authoritative until it either finishes or dies.
    """
    def __init__(self,review_id,token,interval):
        self.review_id=review_id;self.token=token
        self.interval=max(1.0,interval)
        self._stop=threading.Event();self._thread=None

    def __enter__(self):
        self._thread=threading.Thread(target=self._run,daemon=True)
        self._thread.start();return self

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                with Session() as db:
                    renewed=_fenced(db,self.review_id,self.token,lease_expires_at=_lease_deadline())
                    if not renewed:db.rollback();return
                    _attempt_update(db,self.token,heartbeat_at=utcnow())
                    db.commit()
            except Exception:
                return

    def __exit__(self,*exc):
        self._stop.set()
        if self._thread:self._thread.join(timeout=5)
        return False

def _backoff(attempt_no):
    return min(JOB_RETRY_MAX_SECONDS,JOB_RETRY_BASE_SECONDS*(2**max(0,attempt_no-1)))

def publish_fenced(db,review_id,token,snapshot_digest,result):
    """Publish the artifact only while the execution right is still valid.

    The review row is locked for the duration of the atomic replace, so a process
    that lost its lease can never overwrite the winner's file afterwards.
    """
    db.rollback();now=utcnow()
    held=db.execute(
        select(Review.id)
        .where(Review.id==review_id,Review.execution_token==token,
               Review.status==ReviewStatus.running.value,Review.lease_expires_at>now)
        .with_for_update()
    ).scalar_one_or_none()
    if held is None:
        db.rollback();return None
    target,digest=artifacts.publish(review_id,snapshot_digest,result)
    db.commit()
    return target,digest

def complete(db,ticket,result,digest,snapshot_digest=''):
    """Publish is allowed only while this process still owns the execution token."""
    now=utcnow()
    review_id,token=ticket['review_id'],ticket['token']
    values=dict(result=result,status=ReviewStatus.completed.value,
                artifact_checksum=digest,completed_at=now,lease_owner='',lease_expires_at=None,
                next_attempt_at=None,failure_reason='',execution_token='')
    if snapshot_digest:values['snapshot_checksum']=snapshot_digest
    ok=_fenced(db,review_id,token,**values)
    if not ok:
        db.rollback();return False
    _attempt_update(db,token,status=AttemptStatus.completed.value,phase=AttemptPhase.published.value,
                    finished_at=now,heartbeat_at=now,error='')
    db.commit();return True

def fail(db,ticket,error,retryable=True):
    """Record a failed attempt. Retries are bounded by max_attempts."""
    now=utcnow()
    review_id,token=ticket['review_id'],ticket['token']
    max_attempts=max(1,int(ticket.get('max_attempts') or JOB_MAX_ATTEMPTS))
    attempt_no=int(ticket['attempt_no'])
    message=str(error)[:800] or error.__class__.__name__
    should_retry=retryable and attempt_no<max_attempts
    if not should_retry and retryable:
        message=f'{message}（可重试故障已用完 {max_attempts} 次尝试）'
    values={'lease_owner':'','lease_expires_at':None,'failure_reason':message,'execution_token':''}
    if should_retry:
        values.update(status=ReviewStatus.queued.value,
                      next_attempt_at=now+timedelta(seconds=_backoff(attempt_no)))
    else:
        values.update(status=ReviewStatus.failed.value,next_attempt_at=None)
    if not _fenced(db,review_id,token,**values):
        db.rollback();return 'fenced'
    _attempt_update(db,token,status=AttemptStatus.failed.value,phase=AttemptPhase.failed.value,
                    finished_at=now,heartbeat_at=now,error=message)
    db.commit()
    if should_retry:enqueue(review_id)
    return 'retry' if should_retry else 'failed'

def recover(db):
    """Requeue accepted work after a queue outage or worker restart.

    Uses the original review row and its frozen snapshot; nothing is re-read from
    inspections. Returns ids that should be woken up.
    """
    now=utcnow()
    due=db.scalars(
        select(Review)
        .where(Review.status.in_([ReviewStatus.queued.value,ReviewStatus.running.value]))
        .where(or_(Review.lease_expires_at.is_(None),Review.lease_expires_at<=now))
        .where(or_(Review.next_attempt_at.is_(None),Review.next_attempt_at<=now))
        .order_by(Review.id)
    ).all()
    touched=[]
    for row in due:
        if row.status==ReviewStatus.running.value and row.lease_expires_at is not None and aware(row.lease_expires_at)>now:
            continue
        db.execute(update(ReviewAttempt)
                   .where(ReviewAttempt.review_id==row.id,ReviewAttempt.status==AttemptStatus.running.value)
                   .values(status=AttemptStatus.abandoned.value,phase=AttemptPhase.abandoned.value,
                           finished_at=now,error='执行进程中断，任务已自动恢复排队'))
        row.execution_token='';row.lease_owner='';row.lease_expires_at=None;row.next_attempt_at=None
        row.status=ReviewStatus.queued.value
        touched.append(row.id)
    db.commit()
    return touched

def repair_completed(db):
    """Ensure every finished review has a verified artifact before release."""
    rows=db.scalars(select(Review).where(Review.status.in_([ReviewStatus.completed.value,
                                                            ReviewStatus.approved.value]))).all()
    repaired=[]
    for row in rows:
        ok,_=artifacts.verify(row)
        if ok:continue
        ok,_=artifacts.ensure_artifact(row)
        if ok:repaired.append(row.id)
    if repaired:db.commit()
    return repaired

def run_attempt(review_id,worker_id):
    """Claim and execute one delivery. Returns a short outcome for tests and logs."""
    with Session() as db:
        ticket=claim(db,review_id,worker_id)
        if not ticket:return 'skipped'
        token=ticket['token']
        try:
            saved_digest=snapshot_digest_from_row(db,review_id)
            with _LeaseHeartbeat(review_id,token,JOB_LEASE_SECONDS/3):
                return _execute(db,review_id,ticket,saved_digest)
        except PermanentError as e:
            db.rollback();return fail(db,ticket,e,retryable=False)
        except Exception as e:
            db.rollback()
            return fail(db,ticket,e,retryable=True)


def snapshot_digest_from_row(db,review_id):
    return db.get(Review,review_id).snapshot_checksum or ''


def _execute(db,review_id,ticket,saved_digest):
    token=ticket['token']
    review=db.get(Review,review_id)
    snapshot=review.snapshot or []
    saved_digest=saved_digest or artifacts.snapshot_checksum(snapshot)
    if artifacts.snapshot_checksum(snapshot)!=saved_digest:
        raise PermanentError('冻结样本校验值与受理时不一致，拒绝继续计算')
    if not set_phase(db,review_id,token,AttemptPhase.computing.value):
        return 'fenced'
    result=diagnose(snapshot)
    if not set_phase(db,review_id,token,AttemptPhase.writing.value):
        return 'fenced'
    published=publish_fenced(db,review_id,token,saved_digest,result)
    if not published:return 'fenced'
    path,digest=published
    if not set_phase(db,review_id,token,AttemptPhase.verifying.value):
        return 'fenced'
    # Read the file back and compare it with the frozen snapshot and statistics
    # before the review is allowed to look complete.
    ok,reason=artifacts.verify_payload(review_id,path,digest,saved_digest,result)
    if not ok:raise IOError(f'结果文件校验失败：{reason}')
    if not complete(db,ticket,result,digest,saved_digest):return 'fenced'
    return 'completed'
