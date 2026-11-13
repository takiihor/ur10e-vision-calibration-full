/* Inspect accepted IPC work; only a new explicit click can answer its bound prompt. */
(() => {
  const el=id=>document.getElementById(id), token=document.querySelector('meta[name="module-token"],meta[name="teach-token"]').content;
  let current=null, replying=false, connected=false, pollGeneration=0, ownerActive, discovery, shownPrompt, ownerReason='', mcuReason='';
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  async function api(path,body) {
    const response=await fetch(path,{headers:{'X-Module-Token':token,...(body?{'Content-Type':'application/json'}:{})},
      ...(body?{method:'POST',body:JSON.stringify(body)}:{})});
    const value=await response.json();if(!response.ok){const error=new Error(value.error||'Request failed.');error.status=response.status;throw error;}return value;
  }
  function render(job) {
    const manual=job.manual||job.result?.manual||{}, prompt=manual.prompt;
    const stage=manual.stage||'preflight', phases={awaiting_plan:'Review the route. Approve the plan to continue.',awaiting_grip:'Use the MCU buttons to grip. Check the item, then confirm held.',awaiting_release:'Use the MCU buttons to release. Check the item, then confirm released.'};
    el('manual-job-status').dataset.stage=stage;
    el('manual-job-status').dataset.jobId=job.id;
    el('manual-job-status').textContent=`${manual.operation||'Operation'} · ${job.state} · ${phases[stage]||stage.replaceAll('_',' ')} · item: ${(manual.item_state||'unknown').replaceAll('_',' ')}${job.error?' · '+job.error:''}`;
    if(manual.repeat){const r=manual.repeat;el('manual-job-status').textContent=`Automatic test · ${job.state} · ${r.completed_transfers}/${r.transfers} commanded transfers completed · ${r.source||'A'} → ${r.destination||'B'} · ${manual.operation||'REPEAT'} · ${stage.replaceAll('_',' ')} · commanded rod endpoint: ${r.commanded_rod_endpoint||'unknown'} (unverified)${job.error?' · '+job.error:''}`;}
    if(manual.robot_step)el('manual-job-status').textContent=`Robot step · ${job.state} · ${stage==='awaiting_plan'?'Review the exact step plan.':stage.replaceAll('_',' ')}${job.error?' · '+job.error:''}`;
    el('manual-reply-confirmed').closest('label').style.display=manual.repeat?'none':'';
    el('manual-reply').style.display=manual.repeat?'none':'';
    el('manual-job-details').textContent=JSON.stringify(manual,null,2);
    const start=manual.command_start||manual.route?.above, target=manual.command_target||manual.route?.endpoint;
    const taught=manual.taught_pick_tcp_pose_m_rad||manual.route?.endpoint;
    el('manual-route-summary').textContent=start&&target&&taught?
      `Stopped start Z ${(start[2]*1000).toFixed(3)} mm; taught endpoint Z ${(taught[2]*1000).toFixed(3)} mm; descent ${((manual.route.above[2]-taught[2])*1000).toFixed(3)} mm; speed ${manual.speed_mm_s} mm/s. ${manual.operation==='DESCEND'?'Separate descent only; no grip or automatic withdrawal.':manual.repeat?'Automatic MCU command, then straight +100 mm withdrawal.':'Manual grip/release boundary, then straight +100 mm withdrawal.'} Hold actual stopped XY/orientation on each vertical leg.`:'Awaiting the fresh reviewed route.';
    if(manual.robot_step){const s=manual.robot_step,pose=p=>p.map((v,i)=>(i<3?v*1000:v).toFixed(4)).join(', ');
      el('manual-route-summary').textContent=`${s.axis.length===1?'Base '+s.axis.toUpperCase():'Tool-local '+s.axis[1].toUpperCase()+' rotation'}: ${s.increment} ${s.axis.length===1?'mm':'degrees'}. Joint speed ${s.joint_speed_deg_s.toFixed(2)} degrees/s.\nTCP units: XYZ mm, then rotation-vector radians.\nStart: ${pose(s.start_tcp_pose_m_rad)}.\nTarget: ${pose(s.target_tcp_pose_m_rad)}.\nThe IPC checks the joint path after approval. The path can curve and change TCP height or orientation. Workspace bounds apply to the start and endpoint. Details contain the frozen joints, TCP, scene, model and bounds.`;}
    el('manual-reply-label').textContent=prompt?.phase==='plan'?'I reviewed this exact route, workspace, speed and scene. The area is clear.':
      prompt?.phase==='grip'?'I checked that the item is held.':
      prompt?.phase==='release'?'I checked that the item is released.':'No confirmation needed.';
    if(manual.robot_step&&prompt?.phase==='plan')el('manual-reply-label').textContent='I checked the active TCP, endpoints, speed, scene, model and bounds. The tool/item fit the scene. The area is clear. The IPC must check the path before motion.';
    el('manual-reply').textContent=prompt?.phase==='plan'?'Approve plan / start movement':prompt?.phase==='grip'?'Confirm grip / lift 100 mm':prompt?.phase==='release'?'Confirm release / retract 100 mm':'Confirm current phase once';
    if(manual.robot_step&&prompt?.phase==='plan')el('manual-reply').textContent='Approve endpoint / move';
    const reason=mcuReason||ownerReason;
    if(reason)el('manual-reply-label').textContent+=' '+reason;
    el('manual-reply').title=reason;
    el('manual-reply').disabled=!connected||replying||!prompt||!!reason||!el('manual-reply-confirmed').checked;
    el('manual-stop').disabled=!connected||!['running','waiting'].includes(job.state);
    const promptKey=prompt?.id&&`${job.id}:${prompt.id}`;
    if(promptKey&&shownPrompt!==promptKey){shownPrompt=promptKey;el('current-operation').scrollIntoView({block:'start'});}
  }
  async function track(id) {
    const generation=++pollGeneration;
    try {
      while(generation===pollGeneration&&el('manual-current-job').value.trim()===id) {
        try {
          const job=await api('/api/jobs/'+encodeURIComponent(id));
          if(generation!==pollGeneration||el('manual-current-job').value.trim()!==id)return;
          if(JSON.stringify(current?.manual?.prompt)!==JSON.stringify(job.manual?.prompt))el('manual-reply-confirmed').checked=false;
          current=job;connected=true;render(job);
          if(!['running','waiting'].includes(job.state))break;
        } catch(error) {
          if(generation!==pollGeneration)return;
          connected=false;el('manual-reply-confirmed').checked=false;
          el('manual-reply').disabled=true;el('manual-stop').disabled=true;
          el('manual-job-status').textContent='Connection unavailable. IPC may continue accepted work. Use the pendant Stop if needed. Reconnect only inspects; do not submit a replacement. '+error.message;
        }
        await pause(300);
      }
    } finally { /* Each inspection owns its generation; late requests have no authority. */ }
  }
  document.addEventListener('manual-owner-status',event=>{
    const owner=event.detail;
    ownerReason=!owner?'Connection unavailable. Inspect the operation.':owner.mcu_pending?'MCU request pending. Wait for its reply.':owner.execution_uncertain?'Execution is uncertain. Check the physical state.':owner.storage_error?'Job storage failed.':'';
    if(current)render(current);
  });
  document.addEventListener('mcu-request-status',event=>{mcuReason=event.detail||'';if(current)render(current);});
  // IPC status is observation only; discovery never submits or answers work.
  window.inspectActiveManualJob=()=>{
    if(discovery)return discovery;
    discovery=(async()=>{
      const generation=pollGeneration;
      const status=await api('/api/status'), active=status.owner.active;
      const job=active?await api('/api/jobs/'+encodeURIComponent(active)):null;
      const manual=job?.manual&&['running','waiting'].includes(job.state)?job:null;
      // A newer explicit inspection owns the panel, even if this lookup finishes later.
      if(generation!==pollGeneration)return manual;
      document.dispatchEvent(new CustomEvent('manual-owner-status',{detail:status.owner}));
      if(manual&&ownerActive!==active){
        ownerActive=active;
        ++pollGeneration;current=null;connected=false;
        el('manual-reply-confirmed').checked=false;
        el('manual-reply').disabled=true;el('manual-stop').disabled=true;
        el('manual-current-job').value=active;
        localStorage.setItem('ano-manual-inspect-id',active);
        track(active);
      }
      if(!active)ownerActive=null;
      return manual;
    })().finally(()=>{discovery=null;});
    return discovery;
  };
  async function discover(){
    try{await window.inspectActiveManualJob();}
    catch(error){document.dispatchEvent(new CustomEvent('manual-owner-status',{detail:null}));}
  }
  window.startManualJob=async(path,body)=>{
    ++pollGeneration;
    connected=false;current=null;
    el('manual-reply-confirmed').checked=false;
    const id=crypto.randomUUID();
    // No persisted request body, permissions, automatic resend or automatic answer.
    el('manual-current-job').value=id;
    el('manual-job-status').textContent='Submitting '+id+'; if acknowledgement is lost, inspect this ID. Do not create a replacement job.';
    localStorage.setItem('ano-manual-inspect-id',id);
    try {current=await api(path,{request_id:id,...body});connected=true;render(current);track(id);return current;}
    catch(error){el('manual-job-status').textContent=error.message+' Job '+id+'; inspect before any new request.';throw error;}
  };
  el('manual-inspect').addEventListener('click',()=>{current=null;connected=false;el('manual-reply').disabled=true;el('manual-reply-confirmed').checked=false;track(el('manual-current-job').value.trim());});
  el('manual-reply-confirmed').addEventListener('change',()=>{if(current)render(current);});
  el('manual-reply').addEventListener('click',async()=>{
    if(replying||!connected||ownerReason||mcuReason||!current?.manual?.prompt||!el('manual-reply-confirmed').checked)return;
    const prompt=current.manual.prompt, id=current.id;
    replying=true;el('manual-reply-confirmed').checked=false;render(current);
    try {await api('/api/manual-control',{job_id:id,action:'confirm',prompt,value:true});}
    catch(error){el('manual-job-status').textContent=error.message+' No automatic retry.';}
    finally {replying=false;if(current)render(current);}
  });
  el('manual-stop').addEventListener('click',async()=>{
    el('manual-reply-confirmed').checked=false;
    try {await api('/api/manual-control',{job_id:el('manual-current-job').value.trim(),action:'stop'});}
    catch(error){el('manual-job-status').textContent='Stop was not acknowledged. Use the pendant stop. '+error.message;}
  });
  el('manual-current-job').value=localStorage.getItem('ano-manual-inspect-id')||'';
  el('manual-reply-confirmed').checked=false;
  discover();setInterval(discover,1500);
})();
