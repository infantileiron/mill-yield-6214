import hashlib, json, os
from pathlib import Path
from src.config.settings import ARTIFACTS_DIR
from src.services.statistics import diagnose

ARTIFACT_FORMAT=1


def artifacts_dir():
    path=Path(ARTIFACTS_DIR)
    path.mkdir(parents=True,exist_ok=True)
    return path


def artifact_path(review_id):
    return artifacts_dir()/f'review-{int(review_id)}.json'


def snapshot_checksum(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def canonical_result(result):
    return json.dumps(result,sort_keys=True,ensure_ascii=False,separators=(',',':'))


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def artifact_bytes(review_id,snapshot_digest,result):
    payload={'format':ARTIFACT_FORMAT,'review_id':int(review_id),'snapshot_checksum':snapshot_digest,'result':result}
    return json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')


def _write_verified(data,tmp,target):
    """Write, flush, fsync and read back the payload before it becomes visible."""
    try:
        with open(tmp,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        with open(tmp,'rb') as f:
            if f.read()!=data:raise IOError('结果文件回读内容不完整')
        if sha256_hex(Path(tmp).read_bytes())!=sha256_hex(data):raise IOError('结果文件回读校验值不一致')
        os.replace(tmp,target)
        try:
            fd=os.open(str(Path(target).parent),os.O_DIRECTORY);os.fsync(fd);os.close(fd)
        except OSError:
            pass
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def publish(review_id,snapshot_digest,result):
    """Atomically publish the result file. Re-publishing identical bytes is a no-op."""
    data=artifact_bytes(review_id,snapshot_digest,result)
    digest=sha256_hex(data)
    target=artifact_path(review_id)
    try:
        if target.exists() and sha256_hex(target.read_bytes())==digest:
            return target,digest
    except OSError:
        pass
    _write_verified(data,target.with_name(f'.{target.name}.{digest[:16]}.tmp'),target)
    return target,digest


def _read_payload(path):
    try:
        data=path.read_bytes()
    except FileNotFoundError:return None,'结果文件不存在'
    except OSError as e:return None,f'结果文件无法读取：{e}'
    try:
        return data,json.loads(data.decode('utf-8'))
    except Exception:return None,'结果文件格式损坏'


def verify_payload(review_id,path,expected_digest,snapshot_digest,result):
    """Check artifact bytes against an expected digest, frozen snapshot and statistics.

    Used both before a result becomes visible and on every release attempt.
    """
    data,payload=_read_payload(path)
    if data is None:return False,payload
    if sha256_hex(data)!=expected_digest:return False,'结果文件校验值不一致'
    if not isinstance(payload,dict) or payload.get('format')!=ARTIFACT_FORMAT:
        return False,'结果文件版本不受支持'
    if payload.get('review_id')!=int(review_id):return False,'结果文件与复核单不匹配'
    if snapshot_digest and payload.get('snapshot_checksum')!=snapshot_digest:
        return False,'冻结样本校验值不一致'
    stored=payload.get('result') or {}
    if not stored.get('checksum'):return False,'结果文件缺少统计校验值'
    saved=result or {}
    if stored.get('checksum')!=saved.get('checksum'):return False,'统计校验值与结果文件不一致'
    if canonical_result(stored)!=canonical_result(saved):return False,'统计结果与结果文件不一致'
    return True,None


def verify(review):
    """Release gate: the artifact must match the saved statistics and frozen snapshot."""
    if getattr(review,'status','') not in ('completed','approved'):
        return False,'复核单尚未完成计算'
    expected=getattr(review,'artifact_checksum','') or ''
    if not expected:return False,'结果文件尚未登记校验值'
    return verify_payload(review.id,artifact_path(review.id),expected,
                          getattr(review,'snapshot_checksum','') or '',
                          getattr(review,'result','') or {})


def ensure_artifact(review):
    """Rebuild the artifact only when it is missing or is a legacy bare-result file.

    A present-but-mismatched envelope is never silently overwritten: it means either
    tampering or storage corruption and must be investigated before release.
    """
    ok,_=verify(review)
    if ok:return True,None
    path=artifact_path(review.id)
    legacy=False
    if path.exists():
        # Files written before the envelope existed hold only the result JSON. They
        # are upgraded only when their content still equals the saved statistics;
        # any other mismatch means tampering or corruption and is refused.
        try:
            raw=json.loads(path.read_text('utf-8'))
            legacy=(isinstance(raw,dict) and 'format' not in raw
                    and canonical_result(raw)==canonical_result(review.result or {}))
        except Exception:
            legacy=False
        if not legacy:
            return False,'结果文件存在但与已保存的统计或校验值不一致，请人工核查'
    if not getattr(review,'result','') or not getattr(review,'snapshot',''):
        return False,'缺少可用的统计结果或冻结样本'
    try:
        recomputed=diagnose(review.snapshot)
    except Exception as e:
        return False,f'冻结样本重算失败：{e}'
    if recomputed.get('checksum')!=(review.result or {}).get('checksum'):
        return False,'冻结样本重算与已保存统计不一致，拒绝重建结果文件'
    if not review.snapshot_checksum:
        review.snapshot_checksum=snapshot_checksum(review.snapshot)
    _,digest=publish(review.id,review.snapshot_checksum,review.result)
    review.artifact_checksum=digest
    ok,reason=verify(review)
    return ok,reason
