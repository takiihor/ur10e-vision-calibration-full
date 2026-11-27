/* Manual hardware requests require explicit operator declarations and plan approval. */
(() => {
  const el=id=>document.getElementById(id), token=document.querySelector('meta[name="module-token"]').content;
  let capability=null, ownerStatus=null, posting=false, generation=0, recipe=null;
  let travel=null, lastUrgent=null, agvApproval=null, liveAgv=null;
  const urgentByCommand=new Map();
  const stations=['a','b'].map(end=>({end, field:el('repeat-station-'+end), project:null, record:null,
    edited:false, loading:false}));
  const stationFields=['repeat-station-a','repeat-station-b','repeat-agv-timeout','repeat-agv-speed-cap'];
  const display=(id,value)=>{if(el(id).textContent!==value)el(id).textContent=value;};
  const message=text=>{el('manual-message').textContent=text;};
  const fields=['project','calibration','manifest','manual-pick-tag','manual-place-tag','manual-setup','manual-scene',
    'manual-joint-min','manual-joint-max','manual-joint-source','manual-workspace-min','manual-workspace-max',
    'manual-speed','manual-joint-speed','manual-item-state'];
  function numbers(id,n,scale=1) {
    const text=el(id).value.trim(), values=text.split(/[\s,]+/).map(Number);
    if(!text||values.length!==n||!values.every(Number.isFinite))throw new Error(id+' needs '+n+' finite values.');
    return values.map(v=>v*scale);
  }
  function data(action) {
    const lower=numbers('manual-joint-min',6,Math.PI/180), upper=numbers('manual-joint-max',6,Math.PI/180);
    return {action,project:el('project').value,calibration:el('calibration').value,manifest:el('manifest').value,
      pick_tag:el('manual-pick-tag').value,place_tag:el('manual-place-tag').value,reuse_taught_offset:el('manual-setup').checked,
      collision_scene:el('manual-scene').value.trim(),tcp_confirmed:el('manual-setup').checked,
      operator_path_approved:el('manual-setup').checked,held_envelope_confirmed:el('manual-setup').checked,
      initial_item_state:el('manual-item-state').value,joint_bounds_rad:lower.map((v,i)=>[v,upper[i]]),
      joint_bounds_source:el('manual-joint-source').value.trim(),workspace_min_m:numbers('manual-workspace-min',3,.001),
      workspace_max_m:numbers('manual-workspace-max',3,.001),speed_mm_s:Number(el('manual-speed').value),
      joint_speed_deg_s:Number(el('manual-joint-speed').value)};
  }
  function ownerReason() {
    if(posting)return 'Request in progress.';
    if(!ownerStatus)return 'Checking IPC ownership; no new operation until connected.';
    if(ownerStatus.active)return 'Current operation owns hardware. Review its status and pending confirmation in this card. Do not submit another operation.';
    if(ownerStatus.storage_error)return 'IPC job storage failed: '+ownerStatus.storage_error+'. New operations are blocked.';
    if(ownerStatus.execution_uncertain)return 'IPC execution is uncertain. '+(ownerStatus.recovery||'Physical reconciliation is required; no automatic fault clearing.');
    if(ownerStatus.closing)return 'IPC service is closing; new operations are blocked.';
    return '';
  }
  function reason(action) {
    const blocked=ownerReason();if(blocked)return blocked;
    const agvReason=window.agvMotion?.reason('single')??'Checking AGV state…';
    if(agvReason)return agvReason;
    if(!capability?.hardware_available)return capability?.detail||'Checking IPC capability.';
    if(!el('manual-scene').value.trim())return 'Select the reviewed collision scene.';
    if(!el('manual-setup').checked)return 'Check this operation’s physical setup and clear path.';
    const state=el('manual-item-state').value;
    if(state!==(action==='PICK'?'operator_confirmed_released':'operator_confirmed_held'))return action==='PICK'?'Confirm Released / no held item before PICK.':'Confirm Held before PLACE.';
    try {const d=data(action);if(!d.joint_bounds_source||d.joint_bounds_rad.some(([a,b])=>a>=b))return 'Enter the reviewed operating joint bounds and source.';
      if(d.workspace_min_m.some((v,i)=>v>=d.workspace_max_m[i]))return 'Workspace minimum must be below maximum.';
      if(!el('manual-speed').checkValidity()||!el('manual-joint-speed').checkValidity())return 'Use vertical speed 1–20 mm/s and joint speed 1–6 degrees/s.';}
    catch(error){return error.message;}
    return '';
  }
  function render() {
    const blocked=ownerReason();
    const off=window.agvMotion?.off('repeat')===true;
    el('repeat-station-a').required=!off;
    const invalid=stationFields.find(id=>!el(id).checkValidity());
    const [stationA,stationB]=stations.map(row=>row.field.valueAsNumber);
    const offCrossing=off&&Number.isFinite(stationA)&&Number.isFinite(stationB)&&stationA!==stationB;
    const reviewWhy=blocked||(!capability?.hardware_available?capability?.detail||'Checking service access.':'')||
      (stations.some(s=>s.loading)?'Reading the teaching project stations.':'')||
      (offCrossing?'AGV switched off requires both endpoints at the same station.':'')||
      (invalid?'Enter station numbers from 1 to 255, timeout from 1 to 600 s and a cap from 500 to 1500 m/h.':'');
    const drive=travel?.allow_agv_in_recipe;
    const crosses=recipe&&recipe.stations.A!==recipe.stations.B;
    const startWhy=(window.agvMotion?.reason('repeat')??'Checking AGV state…')||reviewWhy||(!recipe?'Review the current recipe.':
      crosses&&!drive?'Check-only mode needs both endpoints at the same station.':
      !el('repeat-approved').checked?'Approve the bounded automatic test.':
      crosses&&!el('repeat-agv-confirmed').checked?'Confirm the AGV app mode, clear line and e-stop coverage.':
      recipe.agv_motion!=='off'&&!agvApproval?'Check the live AGV status and approve the test again.':'');
    el('repeat-review').disabled=!!reviewWhy;
    el('repeat-review').title=reviewWhy;
    el('repeat-approved').disabled=!recipe||!!blocked;
    el('repeat-agv-approval').hidden=!drive||!crosses;
    el('repeat-agv-confirmed').disabled=!recipe||!!blocked||!drive||!crosses;
    el('repeat-start').disabled=!!startWhy;
    el('repeat-start').title=startWhy;
    display('repeat-action-reason',[...new Set([reviewWhy,startWhy].filter(Boolean))].join(' '));
    renderStationSources();
    el('manual-prepare').disabled=!!blocked;el('manual-prepare').title=blocked;
    for(const action of ['PICK','PLACE']){const why=reason(action);el('manual-'+action).disabled=!!why;el('manual-'+action).title=why;}
    el('manual-capability').textContent=['PICK','PLACE'].map(action=>action+': '+(reason(action)||'Ready; confirm the exact fresh plan after admission.')).join(' ');
  }
  async function request(path,body) {
    const response=await fetch(path,{method:body?'POST':'GET',headers:{'X-Module-Token':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
    const value=await response.json();if(!response.ok)throw new Error(value.error||'Request failed.');return value;
  }
  function invalidateRecipe(){
    recipe=null;agvApproval=null;el('repeat-approved').checked=false;el('repeat-agv-confirmed').checked=false;
    display('repeat-approval-stations','');
    display('repeat-summary','Settings changed. Review the recipe before Start.');
  }
  document.addEventListener('test-profile-applied',()=>{el('manual-setup').checked=false;invalidateRecipe();loadStations();render();});
  document.addEventListener('agv-motion-status',event=>{
    liveAgv=event.detail;
    if(agvApproval&&(liveAgv.connection_id!==agvApproval.connection_id||liveAgv.stop_generation!==agvApproval.stop_generation)){
      agvApproval=null;
      el('repeat-approved').checked=false;
      el('repeat-agv-confirmed').checked=false;
      display('repeat-message','AGV approval expired. Check the live AGV status and approve the test again.');
    }
    render();
  });
  document.addEventListener('agv-confirmation-change',event=>{
    if(event.detail==='repeat'){generation++;invalidateRecipe();render();}
  });
  document.addEventListener('manual-owner-status',event=>{ownerStatus=event.detail;render();});
  request('/api/manual-pick-place').then(value=>{capability=value;render();}).catch(error=>{capability={detail:error.message};render();});
  try{el('manual-scene').value=localStorage.getItem('ano-manual-scene')||'';}catch{}
  for(const id of fields)el(id).addEventListener('input',()=>{
    generation++;invalidateRecipe();
    if(id==='project')loadStations();
    if(id==='manual-scene'){try{localStorage.setItem('ano-manual-scene',el(id).value);}catch{}}
    if(id!=='manual-setup')el('manual-setup').checked=false;
    render();
  });
  el('manual-prepare').addEventListener('click',async()=>{
    const review=++generation;
    try {
      const body={request_id:crypto.randomUUID(),action:'prepare',project:el('project').value,calibration:el('calibration').value,manifest:el('manifest').value,
        pick_tag:el('manual-pick-tag').value,place_tag:el('manual-place-tag').value,reuse_taught_offset:el('manual-setup').checked};
      let job=await request('/api/manual-pick-place',body);
      while(['running','waiting'].includes(job.state)){await new Promise(r=>setTimeout(r,300));job=await request('/api/jobs/'+encodeURIComponent(job.id));}
      if(review!==generation)return;
      if(job.state!=='done')throw new Error(job.error||job.state);
      el('manual-result').textContent=JSON.stringify(job.result,null,2);message('Saved role provenance reviewed. Hardware still needs a new quality-gated observation.');
    }catch(error){message(error.message+' No automatic retry.');}
  });
  for(const action of ['PICK','PLACE'])el('manual-'+action).addEventListener('click',async()=>{
    if(reason(action))return;
    const body={...data(action),agv_off:window.agvMotion.off('single')};posting=true;el('manual-setup').checked=false;render();
    try {window.agvMotion.submitted('single',await window.startManualJob('/api/manual-pick-place',body));message('Review the fresh route in Current operation.');}
    catch(error){message(error.message);}
    finally {posting=false;render();}
  });
  function repeatBody(action) {
    const body=data(action);
    return {...body,agv_off:window.agvMotion.off('repeat'),place_project:el('repeat-place-project').value.trim(),
      transfers:Number(el('repeat-transfers').value),settle_s:Number(el('repeat-settle').value),
      station_a:el('repeat-station-a').value===''?null:el('repeat-station-a').valueAsNumber, station_b:el('repeat-station-b').value===''?null:el('repeat-station-b').valueAsNumber,
      agv_timeout_s:el('repeat-agv-timeout').valueAsNumber, agv_speed_cap_m_h:el('repeat-agv-speed-cap').valueAsNumber,
      initial_item_state:'operator_confirmed_released',reuse_taught_offset:true,
      operator_path_approved:true,tcp_confirmed:true,held_envelope_confirmed:true};
  }
  function renderStationSources(){
    for(const row of stations){
      const used=row.field.valueAsNumber|| (row.end==='b'?stations[0].field.valueAsNumber:NaN);
      const recorded=row.record?.station;
      const source=row.record?.source==='agv_status'?'AGV status during teaching':'operator entry during teaching';
      const origin=recorded?'Teaching project: station '+recorded+' ('+source+').':'Teaching project has no recorded station.';
      const entry=row.field.value===''?(row.end==='b'&&Number.isFinite(used)?'Using station '+used+' from A.':''):
        row.edited?'Entered station '+used+'.':'Filled from the teaching project.';
      const mismatch=recorded&&Number.isFinite(used)&&used!==recorded;
      const text=row.loading?'Reading teaching project…':row.error?'Teaching project station unavailable. Check its folder.':
        [origin,entry,mismatch?'Warning: station differs from the teaching project.':''].filter(Boolean).join(' ');
      display('repeat-station-'+row.end+'-source',text);
      el('repeat-station-'+row.end+'-source').dataset.state=mismatch?'waiting':'';
    }
  }
  function loadStations(){
    const project=el('project').value.trim();
    stations.forEach(async row=>{
      const name=row.end==='a'?project:el('repeat-place-project').value.trim()||project;
      if(name===row.project)return;
      Object.assign(row,{project:name,record:null,edited:false,error:false,loading:!!name});
      if(!name){row.field.value='';return;}
      try{
        const found=await request('/api/teacher-project?project='+encodeURIComponent(name));
        if(name!==row.project)return;
        row.record=found.agv;
        if(!row.edited)row.field.value=found.agv?.station??'';
      }catch{
        if(name!==row.project)return;
        row.error=true;
        if(!row.edited)row.field.value='';
      }finally{
        if(name===row.project){row.loading=false;render();}
      }
    });
  }
  for(const id of ['repeat-place-project','repeat-transfers','repeat-settle',...stationFields])el(id).addEventListener('input',()=>{
    const row=stations.find(station=>station.field.id===id);
    if(row)row.edited=true;
    if(id==='repeat-place-project')loadStations();
    generation++;invalidateRecipe();render();
  });
  function approveRepeat(){
    const ready=recipe?.agv_motion==='parked'&&liveAgv?.parked&&Number.isInteger(liveAgv.stop_generation)&&el('repeat-approved').checked&&
      (recipe.stations.A===recipe.stations.B||el('repeat-agv-confirmed').checked);
    agvApproval=ready?{connection_id:liveAgv.connection_id,stop_generation:liveAgv.stop_generation}:null;
    render();
  }
  el('repeat-approved').addEventListener('change',approveRepeat);
  el('repeat-agv-confirmed').addEventListener('change',approveRepeat);
  el('repeat-review').addEventListener('click',async()=>{
    if(el('repeat-review').disabled)return;
    const review=++generation;invalidateRecipe();posting=true;render();
    display('repeat-message','Sending recipe for review…');
    try {
      const body=repeatBody('review_repeat');
      let job=await request('/api/manual-pick-place',{request_id:crypto.randomUUID(),...body});
      while(['running','waiting'].includes(job.state)){await new Promise(r=>setTimeout(r,300));job=await request('/api/jobs/'+encodeURIComponent(job.id));}
      if(review!==generation)return;
      if(job.state!=='done')throw new Error(job.error||job.state);
      recipe=job.result;
      const same=recipe.A===recipe.B&&recipe.stations.A===recipe.stations.B,end=same?'A (same tank)':recipe.end;
      display('repeat-approval-stations',recipe.agv_motion==='off'?'Both endpoints are reachable with the AGV switched off.':`I approve A at AGV station ${recipe.stations.A} and B at AGV station ${recipe.stations.B}.`);
      display('repeat-agv-approval-text',recipe.agv_approval);
      const stationA=recipe.stations.A==null?'':' (AGV station '+recipe.stations.A+')';
      const stationB=recipe.stations.B==null?'':' (AGV station '+recipe.stations.B+')';
      const agvPlan=recipe.agv_motion==='off'?' AGV switched off for this job.':
        ` AGV timeout ${recipe.agv_timeout_s} s, set-speed cap ${recipe.agv_speed_cap_m_h} m/h (${recipe.agv_speed_cap_m_h/1000} km/h).`;
      el('repeat-summary').textContent=`${recipe.transfers} commanded transfer(s). A: ${recipe.A}${stationA}. B: ${recipe.B}${stationB}. Final commanded rod endpoint: ${end} (unverified). Saved approach: A ${recipe.approach_mm.A} mm; B ${recipe.approach_mm.B} mm. Lift/retract: 100 mm. Close 1; open 3; settling ${recipe.settle_s} s.${agvPlan} Each fresh path is checked. No per-operation approval pauses.`;
      el('repeat-message').textContent='Recipe reviewed. Check the physical setup, then approve this bounded test.';
    }catch(error){el('repeat-message').textContent=error.message+' No automatic retry.';}
    finally{posting=false;render();}
  });
  el('repeat-start').addEventListener('click',async()=>{
    if(el('repeat-start').disabled)return;
    const body={...repeatBody('REPEAT'),...agvApproval,automatic_test_approved:true,recipe_sha256:recipe.recipe_sha256,
      agv_manual_confirmed:el('repeat-agv-confirmed').checked};
    display('repeat-message','Sending automatic test…');
    posting=true;invalidateRecipe();render();
    try{window.agvMotion.submitted('repeat',await window.startManualJob('/api/manual-pick-place',body));el('repeat-message').textContent='IPC automatic test accepted. Follow Current operation and use Stop to end the test.';}
    catch(error){el('repeat-message').textContent=error.message;}
    finally{posting=false;render();}
  });
  el('repeat-review').setAttribute('aria-describedby','repeat-action-reason repeat-message');
  el('repeat-start').setAttribute('aria-describedby','repeat-action-reason repeat-message');

  function renderAgv(agv){
    const age=agv?.status_age_s, stale=agv?.connected&&(age>=3||agv.stale);
    const report=age!=null?`last status ${age.toFixed(1)} s ago`:'awaiting a status report';
    const machine=agv?.status, observed=machine?` · station ${machine.current_station} · ${machine.stopped&&machine.actual_speed_m_h===0?'stopped':'moving'}`:'';
    display('pick-agv-link',agv?.connected?`AGV ${stale?'link stale':'connected'} · ${agv.mode ?? agv.settings?.mode ?? 'IPC executor'} · ${report}${observed}`:
      'AGV disconnected. Connect on the AGV page.');
    el('pick-agv-link').dataset.state=stale?'failed':'';
    display('pick-agv-alert',stale?'AGV may still be moving. Use its e-stop. Check the radio.':
      machine?.fault_code?`AGV fault ${machine.fault_code} · ${machine.fault_description}. Check the AGV.`:
      machine?.obstacle_stop?'AGV obstacle stop. Check the line before continuing.':'');
    const standard=agv?.connected&&!stale&&machine?.mode===0;
    el('pick-agv-stop').disabled=!agv?.connected&&!agv?.executor_link;
    el('pick-agv-pause').disabled=!agv?.connected||standard||agv?.executor_link;
    display('pick-agv-pause-why',agv?.executor_link?'Use AGV Stop on this installation.':
      standard?'In standard mode, use AGV Stop. Pause requires station-call mode.':'');
    const stop=urgentByCommand.get('stop');
    if(agv?.executor_link&&(agv.stop_unconfirmed||agv.executor_stop)&&stop)renderExecutorStop(stop,agv.executor_stop);
    for(const urgent of urgentByCommand.values())renderUrgent(urgent,agv);
  }
  function renderUrgent(urgent,agv){
    const {wire,connection,tx,text}=urgent;
    if(!tx)return;
    const result=agv?.results?.[wire];
    let detail='', tone='waiting';
    if(!agv?.connected||(connection!=null&&agv.connection_id!==connection)){
      detail='AGV link lost. Use the AGV e-stop if needed.';tone='failed';
    }else if(result?.tx_id>tx){
      detail='A newer AGV command replaced this outcome. Check the AGV.';tone='failed';
    }else if(result?.tx_id===tx){
      if(result.state==='confirmed by status'){
        if(result.confirmed_stopped===true){detail='AGV reports stopped.';tone='done';}
        else detail=(result.wire_command??wire)==='pause'?'AGV reports pause active. Check whether it has stopped.':
          'AGV reports command stop active. Check whether it has stopped.';
      }else if(result.state==='no reply'){
        detail=`No reply after ${result.tries} tries. Use the AGV e-stop if needed.`;tone='failed';
      }else if(result.state==='not confirmed'||result.state==='not checked'){
        detail='Stop state unconfirmed. Use the AGV e-stop if needed.';tone='failed';
      }else if(result.state==='acknowledged late')detail='AGV acknowledged late. Check whether it has stopped.';
      else if(result.ack_elapsed_s!=null)detail='AGV acknowledged. Waiting for a status report.';
      else if(result.remaining_s>0)detail=`Waiting for the AGV… ${Math.ceil(result.remaining_s)} s.`;
      else if(result.state==='waiting for status')detail='Waiting for a status report. Check the radio.';
    }
    if(detail)urgentFeedback(urgent,text+' · '+detail,tone);
  }
  async function refreshAgv(){
    try{renderAgv(await request('/api/agv-link'));}
    catch{display('pick-agv-link','AGV link status unavailable. Check the service connection.');}
    finally{setTimeout(refreshAgv,750);}
  }
  async function refreshTravel(){
    try{
      travel=await request('/api/agv-travel-pose');
      const pose=travel.pose;
      display('repeat-agv-mode',travel.allow_agv_in_recipe?'Approved recipes can call AGV stations.':
        'AGV check-only mode. Both endpoints must use the same station, with the AGV stopped there.');
      display('travel-pose-value',pose?`Saved ${new Date(pose.saved_at).toLocaleString()} · joints (°): ${pose.joints_rad.map(v=>(v*180/Math.PI).toFixed(1)).join(', ')}`:
        'No AGV travel pose saved.');
      display('travel-pose-live',travel.detail);
    }catch{
      display('travel-pose-live','Travel pose unavailable. Check the service connection.');
    }finally{
      render();
      setTimeout(refreshTravel,750);
    }
  }
  function urgentFeedback(urgent,text,tone){
    const ids=['pick-agv-'+urgent.command+'-outcome'];
    if(lastUrgent===urgent)ids.push('pick-agv-outcome');
    for(const id of ids){
      display(id,text);
      el(id).dataset.state=tone;
    }
  }
  function renderExecutorStop(urgent,report){
    if(report?.confirmed)urgentFeedback(urgent,'Stop confirmed by fresh device evidence.','done');
    else if(report?.received)urgentFeedback(urgent,
      (report.halt_attempted?'Halt attempted. ':'Stop received. ')+'Stop unconfirmed. Use the AGV e-stop if needed.','waiting');
    else urgentFeedback(urgent,'Stop unconfirmed. Use the AGV e-stop if needed.','failed');
  }
  async function sendUrgent(command,esc=false){
    const urgent={command};
    lastUrgent=urgent;
    urgentByCommand.set(command,urgent);
    urgentFeedback(urgent,'Sending…','running');
    try{
      const state=await request('/api/agv',{action:'send',command});
      if(urgentByCommand.get(command)!==urgent)return;
      if(state.executor_stop){renderExecutorStop(urgent,state.executor_stop);return;}
      const wire=state.urgent_command??command;
      const {sent_at:sent,tx_id:tx}=state.results[wire];
      const label=command==='stop'?'Stop':'Pause';
      const text=`${label} sent ${new Date(sent).toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3})}.${esc?' (Esc)':''}`;
      Object.assign(urgent,{wire,tx,connection:state.connection_id,text});
      urgentFeedback(urgent,text,'waiting');
    }catch(error){
      if(urgentByCommand.get(command)!==urgent)return;
      urgentFeedback(urgent,error.message==='Connect an AGV transport first.'?'Connect first on the AGV page.':
        'AGV command could not be confirmed. Use its e-stop if needed.','failed');
    }
  }
  for(const command of ['stop','pause'])
    el('pick-agv-'+command).addEventListener('click',()=>sendUrgent(command));
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&!event.repeat){event.preventDefault();sendUrgent('stop',true);}
  });
  document.addEventListener('manual-job-update',event=>{
    const job=event.detail;
    if(!job.manual?.repeat)return;
    if(job.state==='done')display('repeat-message','Automatic test completed. Review the transfer record in Current operation.');
    if(job.state==='failed'||job.state==='interrupted')display('repeat-message',
      'Automatic test ended. '+(job.error||'Check Current operation before starting a new run.'));
  });
  loadStations();
  void refreshAgv();
  void refreshTravel();
  el('manual-setup').checked=false;
  el('manual-item-state').value='';render();
})();
