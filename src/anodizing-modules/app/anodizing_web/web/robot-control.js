/* One explicit click prepares one owned finite step; polling never sends movement. */
(() => {
  const el=id=>document.getElementById(id), token=document.querySelector('meta[name="module-token"],meta[name="teach-token"]').content;
  const buttons=[...document.querySelectorAll('[data-robot-axis]')];
  let capability=null, owner=null, posting=false, defaultsApplied=false, requestJobId=null, awaitingInspection=false, requestError='', inspecting=false, definitiveRejection=false;
  const ids=['robot-ip','robot-tcp','robot-scene','robot-workspace-min','robot-workspace-max','robot-joint-min','robot-joint-max','robot-joint-source','robot-joint-speed','robot-translation-step','robot-rotation-step'];
  async function get(){const r=await fetch('/api/robot-control',{headers:{'X-Module-Token':token}});const v=await r.json();if(!r.ok)throw Error(v.error||'Robot control unavailable.');return v;}
  function values(id,count,scale=1){const t=el(id).value.trim(),v=t.split(/[\s,]+/).map(Number);if(!t||v.length!==count||!v.every(Number.isFinite))throw Error(id+' needs '+count+' finite values.');return v.map(x=>x*scale);}
  function body(axis,increment){
    const lower=values('robot-joint-min',6,Math.PI/180),upper=values('robot-joint-max',6,Math.PI/180),tcp=values('robot-tcp',6);
    return {action:'STEP',axis,increment,robot_ip:el('robot-ip').value.trim(),tcp:tcp.map((v,i)=>i<3?v/1000:v),
      collision_scene:el('robot-scene').value.trim(),workspace_min_m:values('robot-workspace-min',3,.001),workspace_max_m:values('robot-workspace-max',3,.001),
      joint_bounds_rad:lower.map((v,i)=>[v,upper[i]]),joint_bounds_source:el('robot-joint-source').value.trim(),joint_speed_deg_s:Number(el('robot-joint-speed').value)};
  }
  function reason(){
    if(posting)return 'Step request in progress.';
    if(requestError)return requestError;
    if(awaitingInspection)return 'Inspect the accepted step in Current operation before another request. '+(requestJobId||'');
    if(!capability)return 'Checking IPC robot control capability.';
    if(!capability.enabled)return capability.detail||'Robot control is disabled.';
    if(!owner)return 'Checking IPC ownership.';
    if(owner.active||owner.mcu_pending)return 'Current operation owns hardware. Inspect it before another step.';
    if(owner.execution_uncertain||owner.storage_error||owner.closing)return 'Execution, storage or service state blocks new steps. Inspect Current operation.';
    try{const d=body('x',1);if(!d.robot_ip||!d.collision_scene||!d.joint_bounds_source)throw Error('Enter robot, scene and operating bounds source.');
      if(d.joint_bounds_rad.some(([a,b])=>a>=b)||d.workspace_min_m.some((v,i)=>v>=d.workspace_max_m[i]))throw Error('Check the ordered workspace and operating joint bounds.');
      if(!el('robot-joint-speed').checkValidity())throw Error('Use joint speed from 1 to 6 degrees/s.');}
    catch(error){return error.message;}
    return '';
  }
  function render(){const why=reason();for(const b of buttons){b.disabled=!!why;b.title=why;}el('robot-control-message').textContent=why||'Ready. One click prepares one exact plan; it does not start movement.';}
  for(const id of ids)el(id).addEventListener('input',()=>{el('manual-reply-confirmed').checked=false;el('manual-reply').disabled=true;if(definitiveRejection){requestError='';definitiveRejection=false;}render();});
  async function inspectOutcome(){
    if(!awaitingInspection||!requestJobId||inspecting)return;
    inspecting=true;
    try{const r=await fetch('/api/jobs/'+encodeURIComponent(requestJobId),{headers:{'X-Module-Token':token}});
      if(!r.ok)return;
      const job=await r.json();if(job.id!==requestJobId)return;
      requestError='';
      if(['done','failed','interrupted'].includes(job.state)){awaitingInspection=false;requestJobId=null;}
    }finally{inspecting=false;render();}
  }
  document.addEventListener('manual-owner-status',event=>{owner=event.detail;render();inspectOutcome().catch(()=>{});});
  el('manual-inspect').addEventListener('click',()=>inspectOutcome().catch(()=>{}));
  buttons.forEach(button=>button.addEventListener('click',async()=>{
    if(reason())return;
    const axis=button.dataset.robotAxis,increment=Number(button.dataset.robotDirection)*Number(el(axis.length===1?'robot-translation-step':'robot-rotation-step').value);
    const request=body(axis,increment);posting=true;awaitingInspection=true;requestError='';definitiveRejection=false;el('manual-reply-confirmed').checked=false;render();
    try{const job=await window.startManualJob('/api/robot-control',request);requestJobId=job.id;}
    catch(error){requestJobId=el('manual-current-job').value.trim();definitiveRejection=[400,403,404].includes(error.status);
      if(definitiveRejection){awaitingInspection=false;requestJobId=null;}
      requestError=error.message+(definitiveRejection?' Correct the settings before a new explicit request.':' Inspect the stored operation. No automatic retry.');}
    finally{posting=false;render();}
  }));
  get().then(value=>{
    capability=value;
    if(!defaultsApplied){defaultsApplied=true;const d=value.defaults||{},put=(id,v)=>{if(v!==undefined&&v!==null&&!el(id).value.trim())el(id).value=v;};
      put('robot-ip',d.robot_ip);put('robot-tcp',d.tcp?.map((v,i)=>i<3?v*1000:v).join(', '));put('robot-scene',d.collision_scene);
      put('robot-joint-min',d.joint_bounds_rad?.map(p=>p[0]*180/Math.PI).join(', '));put('robot-joint-max',d.joint_bounds_rad?.map(p=>p[1]*180/Math.PI).join(', '));put('robot-joint-source',d.joint_bounds_source);}
    render();
  }).catch(error=>{el('robot-control-message').textContent=error.message;});
  el('manual-reply-confirmed').checked=false;render();
})();
