export const roles={admin:'质量负责人',analyst:'质量分析员',viewer:'观察员'};
export const statuses={queued:'排队中',running:'计算中',completed:'待签核',approved:'已签核',failed:'失败',verifying:'结果校验中'};
export const attemptStatuses={running:'执行中',completed:'已完成',failed:'失败',abandoned:'中断已接管'};
export const attemptPhases={claimed:'已取得执行权',computing:'计算中',writing:'写入结果',verifying:'回读校验',published:'已发布',failed:'失败',abandoned:'已中断'};
export const lines=['一线','二线','三线'];
// A finished review is only shown as releasable once its artifact passed verification.
export const displayStatus=r=>r.status==='completed'&&r.release_ready===false?'verifying':r.status;
