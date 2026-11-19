/* Explicit MCU clicks. Reopening the page only reads saved job evidence. */
(() => {
  const buttons=[...document.querySelectorAll('[data-mcu]')], sendKey='ano-send-mcu', jobKey='ano-job-mcu';
  let pending=JSON.parse(storage.getItem(sendKey)||'null'), requestId=pending?.request_id||storage.getItem(jobKey);
  let posting=false, state, feedback='';
  function clearPending(){pending=null;storage.removeItem(sendKey);}
  function show(value){
    state=value;
    const last=state.last_known;
    const reply=last?`Last request ${last.command}: ${last.status}. Reply: ${JSON.stringify(last.rx_text)}. Hex: ${last.rx_hex||'(empty)'}.${last.fsr?` FSR1: ${last.fsr[0]}; FSR2: ${last.fsr[1]}.`:''} ${last.error||''}`:'No MCU reply recorded.';
    $('mcu-status').textContent=[reply,state.detail,feedback,pending?'Submission outcome unknown. Check this request. Refresh does not resend.':'',requestId?'Request ID: '+requestId+'.':''].filter(Boolean).join(' ');
    for(const button of buttons)button.disabled=posting||(pending?button.dataset.mcu!==pending.body.command:!state.available);
    $('mcu-check').disabled=posting||!requestId;
    document.dispatchEvent(new CustomEvent('mcu-request-status',{detail:posting?'MCU request pending. Wait for its reply.':state.confirmation_detail}));
  }
  async function refresh(){show(await api('/api/mcu'));}
  function fail(error){
    $('mcu-status').textContent=[feedback,error.message,'Reconnect and check the stored request. No automatic resend.'].join(' ');
    buttons.forEach(button=>button.disabled=true);
  }
  buttons.forEach(button=>{
    button.disabled=true;
    button.addEventListener('click',async()=>{
      if(posting||!state)return;
      if(pending&&(pending.body.command!==button.dataset.mcu||pending.body.project!==field('project'))){
        feedback='Check the previous request before changing its command or project.';show(state);return;
      }
      if(button.dataset.mcu!=='0')$('manual-reply-confirmed').checked=false;
      posting=true;feedback='';show(state);
      try{
        if(!pending){
          show(await api('/api/mcu'));
          if(!state.available)throw new Error(state.detail||'MCU commands are unavailable.');
          pending={request_id:crypto.randomUUID(),body:{project:field('project'),command:button.dataset.mcu,parent:state.parent}};
        }
        requestId=pending.request_id;storage.setItem(sendKey,JSON.stringify(pending));
        let job=await api('/api/mcu',{...pending.body,request_id:pending.request_id});
        storage.setItem(jobKey,job.id);clearPending();
        while(['running','waiting'].includes(job.state)){
          await new Promise(resolve=>setTimeout(resolve,300));
          job=await api('/api/jobs/'+encodeURIComponent(job.id));
        }
        if(job.state!=='done')feedback=job.error||job.state;
      }catch(error){
        if(error.status>=400&&error.status<500)clearPending();
        feedback=error.message+' No automatic resend.';
      }finally{posting=false;await refresh().catch(fail);}
    });
  });
  $('mcu-check').addEventListener('click',async()=>{
    if(posting||!requestId)return;
    try{
      const job=await api('/api/jobs/'+encodeURIComponent(requestId));
      clearPending();feedback='Checked request '+job.id+': '+job.state+'. '+(job.error||'');
    }catch(error){feedback=error.message+' No automatic submission.';}
    await refresh().catch(fail);
  });
  document.addEventListener('manual-owner-status',()=>refresh().catch(fail));
  refresh().catch(fail);
})();
