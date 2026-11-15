from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from src.config.settings import JOB_MAX_ATTEMPTS
from src.constants.enums import ReviewStatus
from src.models.entities import Batch,Inspection,Review,ReviewAttempt
from src.services import artifacts,jobs
from src.utils.serialize import record
from src.utils.time import utcnow

def batches(db):
    rows=db.execute(select(Batch,func.count(Inspection.id)).outerjoin(Inspection,Inspection.batch_id==Batch.id).group_by(Batch.id).order_by(Batch.id.desc())).all()
    return [{**record(b),'samples':n} for b,n in rows]
def create(db,model,data):
    obj=model(**data);db.add(obj)
    try:db.commit()
    except IntegrityError:
        db.rollback();raise HTTPException(409,'编号已存在，请使用独立编号')
    return record(obj)
def inspections(db,batch_id=None):
    q=select(Inspection,Batch.code,Batch.line).join(Batch,Batch.id==Inspection.batch_id).order_by(Inspection.id.desc())
    if batch_id:q=q.where(Inspection.batch_id==batch_id)
    return [{**record(i),'batch_code':code,'line':line} for i,code,line in db.execute(q)]
def add_inspection(db,data):
    if not db.get(Batch,data['batch_id']):raise HTTPException(404,'原粮批次不存在')
    return create(db,Inspection,data)

def submit(db,data,user):
    rows=inspections(db)
    if data['line']!='全部':rows=[x for x in rows if x['line']==data['line']]
    if len(rows)<5:raise HTTPException(422,'该范围至少需要 5 条检验记录，请先录入')
    # Freeze the sample set at acceptance. Recalculation always uses this snapshot.
    obj=Review(**data,created_by=user.email,snapshot=rows,
               snapshot_checksum=artifacts.snapshot_checksum(rows),
               max_attempts=JOB_MAX_ATTEMPTS,status=ReviewStatus.queued.value)
    db.add(obj);db.commit()
    # Queue loss must not fail an accepted review: recovery scanning takes over.
    jobs.enqueue(obj.id)
    return review_payload(db,obj)

def review(db,id):
    obj=db.get(Review,id)
    if not obj:raise HTTPException(404,'复核单不存在')
    return obj

def approve(db,id,note,user):
    obj=review(db,id)
    if obj.status!=ReviewStatus.completed.value:
        raise HTTPException(409,'只有已计算且未签核的复核单可以签核')
    # A stale worker that lost its execution right must not sign off a result.
    if obj.execution_token:
        raise HTTPException(409,'该复核单仍在执行中，暂不可签核')
    ok,reason=artifacts.verify(obj)
    if not ok:
        ok,reason=artifacts.ensure_artifact(obj)
        if not ok:raise HTTPException(409,f'结果文件未通过校验，暂不可签核：{reason}')
        db.commit()
    obj.status=ReviewStatus.approved.value
    obj.note=note;obj.approved_by=user.email;obj.approved_at=utcnow()
    db.commit()
    return review_payload(db,obj)

def _release_state(obj):
    """Completion is visible only when the artifact matches the saved statistics."""
    if obj.status not in (ReviewStatus.completed.value,ReviewStatus.approved.value):
        return False,None
    ok,reason=artifacts.verify(obj)
    return ok,reason

def _attempt_payload(a):
    data=record(a)
    data.pop('execution_token',None)  # never expose the fencing token
    return data

def review_payload(db,obj,include_snapshot=True):
    data=record(obj)
    if not include_snapshot:data.pop('snapshot',None)
    data.pop('execution_token',None)  # never expose the fencing token
    data['attempt_count']=int(obj.attempt_count or 0)
    data['max_attempts']=int(obj.max_attempts or JOB_MAX_ATTEMPTS)
    ready,reason=_release_state(obj)
    data['release_ready']=ready
    data['release_blocked_reason']=reason
    data['attempts']=[_attempt_payload(a) for a in db.scalars(select(ReviewAttempt).where(ReviewAttempt.review_id==obj.id).order_by(ReviewAttempt.attempt_no))]
    return data

def attempts(db,id):
    review(db,id)
    return [_attempt_payload(a) for a in db.scalars(select(ReviewAttempt).where(ReviewAttempt.review_id==id).order_by(ReviewAttempt.attempt_no))]
