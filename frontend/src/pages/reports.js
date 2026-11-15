import {table} from '../components/common/table.js';
import {badge} from '../components/common/status.js';
import {esc,fmt} from '../utils/format.js';
import {attemptStatuses,attemptPhases,displayStatus} from '../constants/enums.js';

const stamp=v=>v?esc(String(v).slice(0,19).replace('T',' '))+' UTC':'—';

function elapsed(a){
  if(!a.started_at)return '—';
  const end=a.finished_at?new Date(a.finished_at):new Date();
  const ms=end-new Date(a.started_at);
  if(!Number.isFinite(ms)||ms<0)return '—';
  return (ms/1000).toFixed(1)+' 秒';
}

export function attemptsPanel(r){
  const attempts=r.attempts||[];
  if(!attempts.length)return `<p class="hint">尚无执行记录。任务受理后会在后台排队恢复。</p>`;
  return table(['尝试','执行进程','状态','阶段','开始','耗时','说明'],
    attempts.map(a=>[
      `#${a.attempt_no} / ${r.max_attempts||'—'}`,
      esc(a.worker_id||'—'),
      esc(attemptStatuses[a.status]||a.status),
      esc(attemptPhases[a.phase]||a.phase||'—'),
      stamp(a.started_at),
      elapsed(a),
      esc(a.error||'—')
    ]));
}

function progress(r){
  const used=Number(r.attempt_count||0),max=Number(r.max_attempts||0);
  const retrying=r.status==='queued'&&used>0;
  const parts=[];
  if(r.status==='failed'){
    parts.push(`<div class="empty"><strong>复核失败</strong><br>${esc(r.failure_reason||r.note||'未知原因')}<br><small>已尝试 ${used} / ${max} 次，可重试故障的次数已用完。请核对后重新提交复核。</small></div>`);
  }else if(r.status==='queued'){
    parts.push(`<div class="empty">${retrying?`第 ${used} 次尝试未完成，已自动恢复排队，等待重试（上限 ${max} 次）。`:'任务已受理，等待后台 worker 处理。'}计算使用受理时冻结的样本快照。<br><small>${retrying?esc(r.failure_reason||''):'队列停机或进程中断后会自动继续，无需重新提交。'}</small></div>`);
  }else if(r.status==='running'){
    parts.push(`<div class="empty">后台正在计算第 ${used} / ${max} 次尝试。结果文件完整写入并通过校验后才会显示完成。<br><small>执行进程：${esc(r.lease_owner||'—')} · 租约到期：${stamp(r.lease_expires_at)}</small></div>`);
  }else if(r.status==='completed'&&r.release_ready===false){
    parts.push(`<div class="empty">结果文件尚未通过校验，暂不可签核或导出。<br><small>${esc(r.release_blocked_reason||'等待结果文件落盘校验')}。后台会自动修复并重新校验。</small></div>`);
  }
  return parts.join('');
}

export function reportsPage(s){
  const r=s.selected||s.reviews.find(x=>x.result?.n);
  const ready=r&&r.release_ready!==false;
  return `<header><p class="eyebrow">04 / REVIEW & RELEASE</p><h1>复核报告</h1><p>核对异常样本、写明判断依据，由质量负责人签核后交付产线。</p></header><div class="workspace"><section class="paper"><h2>复核档案</h2><div class="review-list">${s.reviews.map(x=>`<button class="review-choice" data-review="${x.id}"><strong>${esc(x.title)}</strong><small>#${x.id} · ${esc(x.line)} · 尝试 ${x.attempt_count||0}/${x.max_attempts||0}</small>${badge(displayStatus(x))}</button>`).join('')}</div></section><section>${r?`<div class="section-head"><h2>${esc(r.title)}</h2>${badge(displayStatus(r))}</div><p>样本 ${r.result.n||'—'} 条 · 发起人 ${esc(r.created_by)} · ${esc(r.created_at?.slice(0,16).replace('T',' '))} UTC</p>${r.result.n?`${ready?'':`<div class="empty">结果文件尚未通过校验（${esc(r.release_blocked_reason||'等待落盘校验')}），暂不可签核或导出。</div>`}<div class="report-note"><strong>异常样本 ${r.result.outliers.length} 条，均予保留。</strong><p>以下记录位于 IQR 边界之外，请对照原始检验单核对。统计异常不等于质量不合格。</p></div>${table(['样本编号','批次','产线','出粉率','水分'],r.result.outliers.map(o=>[esc(o.sample_code),esc(o.batch_code),esc(o.line),fmt(o.flour_yield)+'%',fmt(o.moisture)+'%']))}<div class="approval"><h3>质量负责人签核</h3>${r.status==='approved'?`<p class="signed">已签核 / ${esc(r.approved_by)}</p><p>${esc(r.note)}</p>`:`<form id="approve-form" data-id="${r.id}"><label>复核意见<textarea name="note" required minlength="5" placeholder="记录异常样本的复测安排与批次处理结论…"></textarea></label><button class="primary" ${s.user.role==='admin'&&r.status==='completed'&&ready?'':'disabled'}>签核复核单</button><small>${r.status==='completed'&&!ready?'结果文件校验通过后才可签核。':'仅质量负责人可签核；分析员可导出待签核版本。'}</small></form>`}</div><button class="outline" id="download" data-id="${r.id}" ${ready?'':'disabled'}>${r.status==='approved'||r.status==='completed'&&ready?'下载完整 CSV（含冻结样本） ↓':'结果校验通过后可下载'}</button><p class="checksum">SHA-256 ${esc(r.result.checksum)}${r.artifact_checksum?'<br>结果文件 SHA-256 '+esc(r.artifact_checksum):''}</p>`:progress(r)}<h3>执行记录</h3>${attemptsPanel(r)}`:'<div class="empty">先到分布诊断建立一份复核单。</div>'}</section></div>`;
}
