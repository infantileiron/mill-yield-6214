/** @typedef {{id:number,code:string,line:string,supplier:string,tonnes:number,received_date:string,samples:number}} Batch */
/** @typedef {{sample_code:string,batch_id:number,flour_yield:number,moisture:number,inspector:string}} Inspection */
/** @typedef {{id:number,title:string,line:string,status:string,result:object,note:string,attempt_count:number,max_attempts:number,release_ready:boolean,release_blocked_reason:string|null,failure_reason:string,artifact_checksum:string,attempts:ReviewAttempt[]}} Review */
/** @typedef {{attempt_no:number,worker_id:string,status:string,phase:string,error:string,started_at:string,finished_at:string|null}} ReviewAttempt */
export const API_VERSION='1';
