"""Durable-review lifecycle tests.

Run with a throwaway PostgreSQL database:
  TEST_DATABASE_URL=postgresql+psycopg://mill@127.0.0.1:55432/mill_test \
  TEST_ARTIFACTS_DIR=/tmp/mill-artifacts pytest -q tests
"""
import json, os, threading, uuid
from datetime import timedelta
os.environ.setdefault('TEST_DATABASE_URL','')
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

TEST_DB=os.environ.get('TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not TEST_DB,reason='TEST_DATABASE_URL not set')


@pytest.fixture(scope='session',autouse=True)
def _env(tmp_path_factory):
    artifacts=str(tmp_path_factory.mktemp('artifacts'))
    os.environ['ARTIFACTS_DIR']=artifacts
    os.environ['DATABASE_URL']=TEST_DB
    os.environ['JOB_RETRY_BASE_SECONDS']='0.05'
    os.environ['JOB_RETRY_MAX_SECONDS']='0.1'
    import src.config.settings as settings  # noqa: F401  (loaded with env in place)
    yield artifacts


@pytest.fixture(scope='session')
def engine(_env):
    from src.config.database import Base,engine
    import src.models.entities  # noqa: F401
    with engine.begin() as conn:conn.execute(text('DROP SCHEMA public CASCADE; CREATE SCHEMA public'))
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def Session(engine):
    return sessionmaker(engine,expire_on_commit=False)


@pytest.fixture()
def db(Session):
    with Session() as s:
        yield s


@pytest.fixture()
def jobs(_env):
    import importlib, src.services.jobs as m
    return importlib.reload(m)


@pytest.fixture()
def artifacts(_env):
    import importlib, src.services.artifacts as m
    return importlib.reload(m)


def make_review(db,**kwargs):
    from src.models.entities import Review
    from src.services import artifacts as art
    rows=kwargs.pop('rows',None) or [
        {'sample_code':f'S{i}','batch_code':'B1','line':'一线','flour_yield':v,'moisture':12.0}
        for i,v in enumerate([70.0,71.0,72.0,73.0,74.0,75.0,95.0])]
    review=Review(title=kwargs.pop('title','测试复核'),line='全部',created_by='analyst@demo.local',
                  snapshot=rows,snapshot_checksum=art.snapshot_checksum(rows),**kwargs)
    db.add(review);db.commit()
    return review


# --- basic completion -------------------------------------------------------
def test_worker_publishes_verified_artifact(db,jobs,artifacts,Session):
    review=make_review(db,max_attempts=3)
    outcome=jobs.run_attempt(review.id,'w1')
    assert outcome=='completed'
    db.expire_all()
    saved=db.get(type(review),review.id)
    assert saved.status=='completed'
    assert saved.result['n']==7
    ok,reason=artifacts.verify(saved)
    assert ok,reason
    assert saved.artifact_checksum and saved.completed_at
    assert saved.attempt_count==1
    from src.models.entities import ReviewAttempt
    attempts=db.scalars(select(ReviewAttempt).where(ReviewAttempt.review_id==review.id)).all()
    assert [(a.attempt_no,a.status,a.phase) for a in attempts]==[(1,'completed','published')]


# --- duplicate delivery must not run twice ---------------------------------
def test_duplicate_delivery_is_claimed_once(Session,jobs,artifacts):
    with Session() as db:review=make_review(db)
    outcomes=[]
    barrier=threading.Barrier(4)
    def run():
        barrier.wait()
        with Session() as s:outcomes.append(jobs.run_attempt(review.id,'dup'))
    threads=[threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads];[t.join() for t in threads]
    assert outcomes.count('completed')==1
    assert all(o in ('completed','skipped') for o in outcomes)
    with Session() as db:
        from src.models.entities import ReviewAttempt
        assert len(db.scalars(select(ReviewAttempt).where(ReviewAttempt.review_id==review.id)).all())==1
        assert db.get(type(review),review.id).attempt_count==1


# --- stale process cannot overwrite ---------------------------------------
def test_lost_execution_right_cannot_publish_or_complete(Session,jobs,artifacts):
    from src.models.entities import Review
    with Session() as db:
        review=make_review(db)
        first=jobs.claim(db,review.id,'old-worker')
        assert first
        old_token=first['token']
        # Simulate the old worker dying and its lease expiring.
        row=db.get(Review,review.id)
        row.lease_expires_at=jobs.utcnow()-timedelta(seconds=1)
        db.commit()
    with Session() as db:
        second=jobs.claim(db,review.id,'new-worker')
        assert second and second['token']!=old_token
    # The old process wakes up holding a stale token.
    with Session() as db:
        stale={'n':1,'checksum':'stale'}
        assert jobs.publish_fenced(db,review.id,old_token,'x',stale) is None
        assert jobs.complete(db,first,stale,'deadbeef') is False
        assert not artifacts.artifact_path(review.id).exists()
        row=db.get(Review,review.id)
        assert row.status=='running' and row.execution_token==second['token']
        assert row.result=={}  # untouched by the stale writer
    # The legitimate owner still finishes cleanly.
    with Session() as db:
        row=db.get(Review,review.id)
        row.status='queued';row.execution_token='';row.lease_expires_at=None;db.commit()
    with Session() as db:assert jobs.run_attempt(review.id,'new-worker')=='completed'
    with Session() as db:
        row=db.get(Review,review.id)
        assert row.status=='completed' and row.result['n']==7


def test_execution_holder_can_finish_after_rival_failed(Session,jobs,artifacts):
    """A takeover attempt that dies releases the lease; the next run completes."""
    from src.models.entities import Review
    with Session() as db:
        review=make_review(db)
        first=jobs.claim(db,review.id,'w1')
    with Session() as db:
        row=db.get(Review,review.id);row.lease_expires_at=jobs.utcnow()-timedelta(seconds=1);db.commit()
    with Session() as db:
        second=jobs.claim(db,review.id,'w2')
        assert second['token']!=first['token']
    # w1 is now stale and must not write.
    with Session() as db:assert jobs.complete(db,first,{'n':1,'checksum':'x'},'x') is False
    # Finish via a fresh run for w2 after releasing the lease.
    with Session() as db:
        row=db.get(Review,review.id);row.status='queued';row.execution_token='';row.lease_expires_at=None;db.commit()
    with Session() as db:assert jobs.run_attempt(review.id,'w2')=='completed'


# --- retries ---------------------------------------------------------------
def test_retryable_failure_retries_then_fails_at_limit(Session,jobs,artifacts,engine,monkeypatch):
    from src.models.entities import Review,ReviewAttempt
    with Session() as db:review=make_review(db,max_attempts=3)
    calls={'n':0}
    real=jobs.diagnose
    def flaky(rows):
        calls['n']+=1
        if calls['n']<3:raise IOError('磁盘写入失败')
        return real(rows)
    monkeypatch.setattr(jobs,'diagnose',flaky)
    outcomes=[]
    for _ in range(3):
        with Session() as db:
            row=db.get(Review,review.id);row.next_attempt_at=None
            row.lease_expires_at=None;db.commit()
        with Session() as db:outcomes.append(jobs.run_attempt(review.id,'w'))
    assert outcomes==['retry','retry','completed']
    with Session() as db:
        row=db.get(Review,review.id)
        assert row.status=='completed' and row.attempt_count==3
        assert len(db.scalars(select(ReviewAttempt).where(ReviewAttempt.review_id==row.id)).all())==3


def test_retry_exhaustion_marks_failed_with_reason(Session,jobs,artifacts,monkeypatch):
    from src.models.entities import Review,ReviewAttempt
    with Session() as db:review=make_review(db,max_attempts=2)
    def always(rows):raise IOError('结果文件写入失败')
    monkeypatch.setattr(jobs,'diagnose',always)
    for _ in range(5):
        with Session() as db:
            row=db.get(Review,review.id);row.next_attempt_at=None;row.lease_expires_at=None;db.commit()
        with Session() as db:jobs.run_attempt(review.id,'w')
    with Session() as db:
        row=db.get(Review,review.id)
        assert row.status=='failed'
        assert '结果文件写入失败' in row.failure_reason
        assert row.attempt_count==2
        assert '2 次尝试' in row.failure_reason
        assert db.scalars(select(ReviewAttempt).where(ReviewAttempt.review_id==row.id)).all()[-1].status=='failed'


# --- recovery --------------------------------------------------------------
def test_recovery_requeues_interrupted_review_and_uses_original_snapshot(Session,jobs,artifacts):
    from src.models.entities import Review,Batch,Inspection
    with Session() as db:
        review=make_review(db)
        original=review.snapshot_checksum
        # Interrupted mid-flight: running with an expired lease.
        row=db.get(Review,review.id)
        row.status='running';row.attempt_count=1;row.execution_token=uuid.uuid4().hex
        row.lease_owner='dead-worker';row.lease_expires_at=jobs.utcnow()-timedelta(seconds=5)
        db.add(Batch(code='LATE-B',line='一线',supplier='x',tonnes=1,received_date='2026-11-15'))
        db.flush()
        # A later inspection must not leak into the frozen sample.
        db.add(Inspection(sample_code='LATE',batch_id=db.scalar(select(Batch.id).where(Batch.code=='LATE-B')),flour_yield=99.0,moisture=12,inspector='x'))
        db.commit()
    with Session() as db:
        ids=jobs.recover(db)
        assert review.id in ids
    with Session() as db:
        row=db.get(Review,review.id)
        assert row.status=='queued' and row.execution_token=='' and row.lease_expires_at is None
    with Session() as db:assert jobs.run_attempt(review.id,'w')=='completed'
    with Session() as db:
        row=db.get(Review,review.id)
        assert row.status=='completed'
        assert row.snapshot_checksum==original
        assert row.result['n']==7
        assert all(s['sample_code']!='LATE' for s in row.snapshot)


def test_recovery_releases_attempt_stuck_running(Session,jobs,artifacts):
    from src.models.entities import Review,ReviewAttempt
    with Session() as db:
        review=make_review(db)
        jobs.claim(db,review.id,'dead')
        row=db.get(Review,review.id);row.lease_expires_at=jobs.utcnow()-timedelta(seconds=5);db.commit()
    with Session() as db:jobs.recover(db)
    with Session() as db:
        attempt=db.scalars(select(ReviewAttempt).where(ReviewAttempt.review_id==review.id)).one()
        assert attempt.status=='abandoned'
        assert db.get(Review,review.id).status=='queued'


# --- artifacts -------------------------------------------------------------
def test_completion_requires_matching_artifact(Session,jobs,artifacts,monkeypatch):
    from src.models.entities import Review
    with Session() as db:review=make_review(db)
    # The saved statistics no longer match what was published.
    monkeypatch.setattr(jobs.artifacts,'verify_payload',
                        lambda *a,**k:(False,'结果文件校验值不一致'))
    with Session() as db:outcome=jobs.run_attempt(review.id,'w')
    assert outcome=='retry'
    with Session() as db:
        row=db.get(Review,review.id)
        assert row.status!='completed'
        assert '校验' in row.failure_reason


def test_missing_artifact_is_repaired_without_re_reading_samples(Session,jobs,artifacts):
    with Session() as db:review=make_review(db)
    with Session() as db:assert jobs.run_attempt(review.id,'w')=='completed'
    path=artifacts.artifact_path(review.id)
    path.unlink()
    with Session() as db:
        row=db.get(type(review),review.id)
        ok,reason=artifacts.verify(row)
        assert not ok
        assert jobs.repair_completed(db)==[review.id]
        ok,reason=artifacts.verify(db.get(type(review),review.id))
        assert ok,reason


def test_artifact_write_retries_without_duplicate_publication(Session,jobs,artifacts,monkeypatch):
    with Session() as db:review=make_review(db,max_attempts=3)
    real=jobs.artifacts.publish
    attempts={'n':0}
    def flaky(*a,**k):
        attempts['n']+=1
        if attempts['n']==1:raise OSError('磁盘空间不足')
        return real(*a,**k)
    monkeypatch.setattr(jobs.artifacts,'publish',flaky)
    with Session() as db:assert jobs.run_attempt(review.id,'w')=='retry'
    with Session() as db:
        row=db.get(type(review),review.id);row.next_attempt_at=None;db.commit()
    with Session() as db:assert jobs.run_attempt(review.id,'w')=='completed'
    with Session() as db:
        row=db.get(type(review),review.id)
        assert row.status=='completed'
        assert len(list(artifacts.artifacts_dir().glob(f'review-{review.id}.json')))==1
        assert not list(artifacts.artifacts_dir().glob('.*.tmp'))


def test_export_and_approve_blocked_until_artifact_verifies(Session,jobs,artifacts):
    from fastapi import HTTPException
    from src.services import business
    from src.models.entities import User
    with Session() as db:review=make_review(db)
    with Session() as db:jobs.run_attempt(review.id,'w')
    with Session() as db:
        row=db.get(type(review),review.id)
        artifacts.artifact_path(row.id).write_text('{"broken":true}')
        with pytest.raises(HTTPException) as err:
            business.approve(db,row.id,'核对异常样本，安排复测',User(email='admin@demo.local',role='admin'))
        assert '校验' in str(err.value.detail)
        assert db.get(type(review),row.id).status=='completed'
        ok,reason=business._release_state(db.get(type(review),row.id))
        assert not ok and reason


def test_approve_requires_verified_completed_review(Session,jobs,artifacts):
    from src.services import business
    from src.models.entities import User
    with Session() as db:review=make_review(db)
    with Session() as db:jobs.run_attempt(review.id,'w')
    admin=User(email='admin@demo.local',role='admin')
    with Session() as db:
        payload=business.approve(db,review.id,'核对异常样本，安排复测',admin)
        assert payload['status']=='approved' and payload['approved_at']
        assert payload['release_ready'] is True


def test_completed_review_without_checksum_is_not_release_ready(Session,jobs,artifacts):
    from src.services import business
    with Session() as db:
        review=make_review(db,status='completed')
    with Session() as db:
        row=db.get(type(review),review.id)
        ready,reason=business._release_state(row)
        assert exposed_ready(db,row)==False
        assert reason and '尚未登记校验值' in reason


def exposed_ready(db,row):
    from src.services import business
    return business.review_payload(db,row)['release_ready']


# --- legacy artifacts ------------------------------------------------------
def test_legacy_bare_result_artifact_is_upgraded_and_exportable(Session,artifacts):
    from src.services import business
    with Session() as db:review=make_review(db)
    # Emulate the pre-upgrade worker: bare result JSON, no envelope, no checksums.
    result=__import__('src.services.statistics',fromlist=['diagnose']).diagnose(review.snapshot)
    path=artifacts.artifact_path(review.id)
    path.write_text(__import__('json').dumps(result,ensure_ascii=False),encoding='utf-8')
    from src.models.entities import Review
    with Session() as db:
        row=db.get(Review,review.id)
        row.status='completed';row.result=result
        row.snapshot_checksum='';row.artifact_checksum=''
        db.commit()
    with Session() as db:
        row=db.get(Review,review.id)
        assert business._release_state(row)[0] is False
        assert artifacts.ensure_artifact(row)[0] is True
        db.commit()
    with Session() as db:
        row=db.get(Review,review.id)
        ready,reason=business._release_state(row)
        assert ready,reason
        assert row.snapshot_checksum and row.artifact_checksum
        payload=__import__('json').loads(path.read_text('utf-8'))
        assert payload['format']==artifacts.ARTIFACT_FORMAT


def test_tampered_envelope_is_not_silently_rebuilt(Session,jobs,artifacts):
    with Session() as db:review=make_review(db)
    with Session() as db:jobs.run_attempt(review.id,'w')
    path=artifacts.artifact_path(review.id)
    data=path.read_bytes();path.write_bytes(data.replace(b'"n":',b'"n" :',1))
    with Session() as db:
        row=db.get(type(review),review.id)
        ok,reason=artifacts.ensure_artifact(row)
        assert not ok and '人工核查' in reason


# --- lease heartbeat -------------------------------------------------------
def test_slow_computation_keeps_lease_and_cannot_be_taken_over(Session,jobs,artifacts,monkeypatch):
    from src.models.entities import Review
    with Session() as db:review=make_review(db)
    real=jobs.diagnose
    def slow(rows):
        # Longer than the lease; the heartbeat must keep extending it.
        import time as _t;_t.sleep(2.2)
        return real(rows)
    monkeypatch.setattr(jobs,'diagnose',slow)
    monkeypatch.setattr(jobs,'JOB_LEASE_SECONDS',1)
    outcomes=[]
    def rival():
        # Wait past the initial one-second lease so only the heartbeat can still
        # be keeping this attempt alive.
        import time as _t;_t.sleep(1.6)
        with Session() as db:outcomes.append(jobs.claim(db,review.id,'rival'))
    import threading as _th
    t=_th.Thread(target=rival);t.start()
    with Session() as db:result=jobs.run_attempt(review.id,'owner')
    t.join()
    assert result=='completed'
    assert outcomes==[None],outcomes  # rival never obtained execution rights
    with Session() as db:
        row=db.get(Review,review.id)
        assert row.status=='completed' and row.execution_token==''
