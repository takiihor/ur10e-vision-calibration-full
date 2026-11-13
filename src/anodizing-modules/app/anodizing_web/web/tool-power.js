/* Integration snippet: existing api(path,body), field(id), $ and current item selection. */
(() => {
  const buttons=[...document.querySelectorAll('[data-tool-power]')];
  const sendKey='ano-send-tool-power', jobKey='ano-job-tool-power';
  const feedbackKey='ano-tool-power-feedback';
  let posting=false, feedback=localStorage.getItem(feedbackKey)||'', pending=JSON.parse(localStorage.getItem(sendKey)||'null');
  let requestId=pending?.request_id || localStorage.getItem(jobKey);
  function clearPending(){pending=null;localStorage.removeItem(sendKey);}
  function showFailure(error){
    localStorage.setItem(feedbackKey,feedback);
    $('tool-power-status').textContent=[feedback,requestId?'Request ID: '+requestId+'.':'',error.message].filter(Boolean).join(' ');
  }
  buttons.forEach(button=>button.disabled=true);
  function show(state) {
    localStorage.setItem(feedbackKey,feedback);
    const last=state.last_known;
    $('tool-power-status').textContent=last
      ? `Controller tool-port voltage: ${last.controller_tool_output_voltage ?? 'unknown'}${last.controller_tool_output_voltage===null?'':' V'} · ${last.robot_host} · ${last.project} · ${last.observed_at?new Date(last.observed_at*1000).toLocaleString():'no confirmed readback'} (last recorded). ${state.detail}`
      : 'Controller tool-port voltage: unknown. No read requested. '+state.detail;
    if(feedback)$('tool-power-status').textContent+=' '+feedback;
    if(pending)$('tool-power-status').textContent+=' Submission outcome unknown. Inspect this request before another command; refresh never resends.';
    if(requestId)$('tool-power-status').textContent+=' Request ID: '+requestId+'.';
    $('tool-power-check').disabled=posting || !requestId;
    for(const button of buttons)button.disabled=posting || (pending
      ? button.dataset.toolPower!==pending.body.action
      : !(button.dataset.toolPower==='read'?state.read_available:state.write_available));
    const off=buttons.find(button=>button.dataset.toolPower==='off');
    off.disabled ||= !['operator_confirmed_released','no_confirmed_item'].includes($('manual-item-state').value);
  }
  async function refresh(cached){show(cached || await api('/api/tool-power'));} // Cached only; no controller query.
  for(const button of buttons)button.addEventListener('click',async()=>{
    if(posting)return;
    const body={project:field('project'),action:button.dataset.toolPower,item_state:$('manual-item-state').value || 'unknown'};
    // Keep the exact request ID across a transport retry. A different intent cannot replace it.
    if(pending && JSON.stringify(body)!==JSON.stringify(pending.body)){
      $('tool-power-status').textContent='Previous request '+pending.request_id+' outcome unknown. Restore the previous action/project/item selection and retry it first.';return;
    }
    pending ||= {body,request_id:crypto.randomUUID()};
    requestId=pending.request_id;localStorage.setItem(sendKey,JSON.stringify(pending));
    feedback='';localStorage.removeItem(feedbackKey);posting=true;buttons.forEach(b=>b.disabled=true);$('tool-power-check').disabled=true;
    try {
      let job=await api('/api/tool-power',{...pending.body,request_id:pending.request_id});
      requestId=job.id;localStorage.setItem(jobKey,requestId);clearPending();
      while(['running','waiting'].includes(job.state)){
        await new Promise(resolve=>setTimeout(resolve,300));
        job=await api('/api/jobs/'+encodeURIComponent(job.id));
      }
      if(job.state!=='done')feedback=job.error||job.state;
    } catch(error) {
      if(error.status>=400 && error.status<500)clearPending();
      feedback=error.message+' No automatic retry. Check the IPC job outcome before another command.';
    } finally {posting=false;await refresh().catch(showFailure);}
  });
  $('tool-power-check').addEventListener('click',async()=>{
    if(posting || !requestId)return;
    try {
      const job=await api('/api/jobs/'+encodeURIComponent(requestId));
      clearPending();
      feedback='Checked job '+job.id+': '+job.state+'. '+(job.error||'');
    } catch(error) {feedback=error.message+' No automatic submission.';}
    await refresh().catch(showFailure);
  });
  $('manual-item-state').addEventListener('input',()=>refresh().catch(showFailure));
  document.addEventListener('manual-owner-status',()=>refresh().catch(showFailure));
  globalThis.refreshToolPower=refresh; // Call from the main UI's existing cached owner-status refresh.
  refresh().catch(showFailure);
})();
