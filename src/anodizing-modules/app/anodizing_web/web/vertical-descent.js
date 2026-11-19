/* Separate Teacher action. It never changes planar tracking or old Run behavior. */
(() => {
  const el=id=>document.getElementById(id);
  let capability=null, posting=false, alignment='';
  function values(id,n,scale) {
    const text=el(id).value.trim(), nums=text.split(/[\s,]+/).map(Number);
    if(!text||nums.length!==n||!nums.every(Number.isFinite))throw new Error('Enter '+n+' reviewed values for '+id+'.');
    return nums.map(v=>v*scale);
  }
  function inputs() {
    const lo=values('descent-joint-min',6,Math.PI/180), hi=values('descent-joint-max',6,Math.PI/180);
    const lower=values('validation-workspace-min',3,.001), upper=values('validation-workspace-max',3,.001);
    if(lo.some((v,i)=>v>=hi[i])||!el('descent-joint-source').value.trim())throw new Error('Enter the reviewed operating joint bounds and their source.');
    const z=state.project.validation.pick_tcp_pose_m_rad[2];
    if(z<lower[2]||z>upper[2])throw new Error(`Taught Z ${(z*1000).toFixed(3)} mm is outside workspace Z ${(lower[2]*1000).toFixed(3)} to ${(upper[2]*1000).toFixed(3)} mm. No clamping.`);
    return {joint_bounds_rad:lo.map((v,i)=>[v,hi[i]]),joint_bounds_source:el('descent-joint-source').value.trim(),workspace_min_m:lower,workspace_max_m:upper};
  }
  function reason() {
    if(posting)return 'Submitting separate descent.';
    const agvReason=window.agvMotion?.reason('descent')??'Checking AGV state…';
    if(agvReason)return agvReason;
    if(!capability?.hardware_available)return capability?.detail||'Checking reviewed IPC Cartesian capability.';
    if(!connectionOK||dirty||state.requires_setup||state.busy)return 'Complete current setup and wait for a connected idle Teacher.';
    const v=state.project?.validation;
    if(v?.status!=='completed'||v.validation_mode!=='fixed_z')return 'Complete the checked planar alignment for the current capture first.';
    if(state.descent_alignment?.job_id!==teacherJob||state.descent_alignment?.capture_id!==v.id)return 'This alignment was superseded or the IPC restarted. Complete a new alignment before descent.';
    if(!el('validation-collision-scene').value.trim())return 'Select the reviewed collision scene.';
    if(!el('descent-confirmed').checked)return 'Review and separately approve descent to the current taught height.';
    try {inputs();}catch(error){return error.message;}
    if(!el('descent-speed').checkValidity())return 'Vertical speed must be 1–20 mm/s.';
    return '';
  }
  function renderDescent() {
    const v=state.project?.validation, key=teacherJob+':'+v?.id+':'+v?.grasp_sha256;
    if(alignment!==key){alignment=key;el('descent-confirmed').checked=false;}
    el('descent-plan').textContent=v?.pick_tcp_pose_m_rad&&v?.validation_tcp_pose_m_rad?
      `Current taught Z ${(v.pick_tcp_pose_m_rad[2]*1000).toFixed(3)} mm; aligned target Z ${(v.validation_tcp_pose_m_rad[2]*1000).toFixed(3)} mm; planned descent ${((v.validation_tcp_pose_m_rad[2]-v.pick_tcp_pose_m_rad[2])*1000).toFixed(3)} mm. Capture ${v.id}; grasp ${v.grasp_sha256}.`:'No current alignment plan.';
    const why=reason();el('descent-reason').textContent=why||'Ready to request the separate exact descent plan.';el('descent-start').disabled=!!why;
  }
  for(const id of ['descent-speed','descent-joint-min','descent-joint-max','descent-joint-source','validation-collision-scene','validation-workspace-min','validation-workspace-max'])el(id).addEventListener('input',()=>{el('descent-confirmed').checked=false;renderDescent();});
  el('descent-confirmed').addEventListener('change',renderDescent);
  document.addEventListener('agv-motion-status',renderDescent);
  el('descent-start').addEventListener('click',async()=>{
    if(reason())return;
    const p=state.project,v=p.validation;
    const body={action:'vertical_descent',agv_off:window.agvMotion.off('descent'),project:p.project,calibration:p.settings.calibration,manifest:p.settings.target.board_manifest||'',
      alignment_job_id:teacherJob,alignment_capture_id:v.id,marker_unchanged:el('descent-confirmed').checked,operator_vertical_approved:el('descent-confirmed').checked,operator_path_approved:el('descent-confirmed').checked,tcp_confirmed:el('descent-confirmed').checked,
      collision_scene:el('validation-collision-scene').value.trim(),speed_mm_s:Number(el('descent-speed').value),...inputs()};
    posting=true;el('descent-confirmed').checked=false;renderDescent();
    try{window.agvMotion.submitted('descent',await window.startManualJob('/api/teaching',body));}catch(error){el('descent-reason').textContent=error.message;}
    finally{posting=false;renderDescent();}
  });
  moduleRequest('/api/manual-pick-place').then(value=>{capability=value;renderDescent();}).catch(error=>{capability={detail:error.message};renderDescent();});
  el('descent-confirmed').checked=false;
  setInterval(renderDescent,500);
})();
