/* Manual hardware requests require explicit operator declarations and plan approval. */
(() => {
  const el=id=>document.getElementById(id), token=document.querySelector('meta[name="module-token"]').content;
  let capability=null, ownerStatus=null, posting=false, generation=0, recipe=null;
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
    el('repeat-review').disabled=!!blocked||!capability?.hardware_available;
    el('repeat-review').title=blocked||capability?.detail||'';
    el('repeat-approved').disabled=!recipe||!!blocked;
    el('repeat-start').disabled=!!blocked||!recipe||!el('repeat-approved').checked;
    el('repeat-start').title=blocked||(!recipe?'Review the current recipe.':!el('repeat-approved').checked?'Approve the bounded automatic test.':'');
    el('manual-prepare').disabled=!!blocked;el('manual-prepare').title=blocked;
    for(const action of ['PICK','PLACE']){const why=reason(action);el('manual-'+action).disabled=!!why;el('manual-'+action).title=why;}
    el('manual-capability').textContent=['PICK','PLACE'].map(action=>action+': '+(reason(action)||'Ready; confirm the exact fresh plan after admission.')).join(' ');
  }
  async function request(path,body) {
    const response=await fetch(path,{method:body?'POST':'GET',headers:{'X-Module-Token':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
    const value=await response.json();if(!response.ok)throw new Error(value.error||'Request failed.');return value;
  }
  function invalidateRecipe(){recipe=null;el('repeat-approved').checked=false;el('repeat-summary').textContent='Settings changed. Review the recipe before Start.';}
  document.addEventListener('test-profile-applied',()=>{el('manual-setup').checked=false;invalidateRecipe();render();});
  document.addEventListener('manual-owner-status',event=>{ownerStatus=event.detail;render();});
  request('/api/manual-pick-place').then(value=>{capability=value;render();}).catch(error=>{capability={detail:error.message};render();});
  el('manual-scene').value=localStorage.getItem('ano-manual-scene')||'';
  for(const id of fields)el(id).addEventListener('input',()=>{
    generation++;invalidateRecipe();
    if(id==='manual-scene')localStorage.setItem('ano-manual-scene',el(id).value);
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
    const body=data(action);posting=true;el('manual-setup').checked=false;render();
    try {await window.startManualJob('/api/manual-pick-place',body);message('Review the fresh route in Current operation.');}
    catch(error){message(error.message);}
    finally {posting=false;render();}
  });
  function repeatBody(action) {
    const body=data(action);
    return {...body,place_project:el('repeat-place-project').value.trim(),
      transfers:Number(el('repeat-transfers').value),settle_s:Number(el('repeat-settle').value),
      initial_item_state:'operator_confirmed_released',reuse_taught_offset:true,
      operator_path_approved:true,tcp_confirmed:true,held_envelope_confirmed:true};
  }
  for(const id of ['repeat-place-project','repeat-transfers','repeat-settle'])el(id).addEventListener('input',()=>{generation++;invalidateRecipe();render();});
  el('repeat-approved').addEventListener('change',render);
  el('repeat-review').addEventListener('click',async()=>{
    if(ownerReason())return;
    const review=++generation;invalidateRecipe();posting=true;render();
    try {
      const body=repeatBody('review_repeat');
      let job=await request('/api/manual-pick-place',{request_id:crypto.randomUUID(),...body});
      while(['running','waiting'].includes(job.state)){await new Promise(r=>setTimeout(r,300));job=await request('/api/jobs/'+encodeURIComponent(job.id));}
      if(review!==generation)return;
      if(job.state!=='done')throw new Error(job.error||job.state);
      recipe=job.result;
      const same=recipe.A===recipe.B,end=same?'A (same tank)':recipe.end;
      el('repeat-summary').textContent=`${recipe.transfers} commanded transfer(s). A: ${recipe.A}. B: ${recipe.B}. Final commanded rod endpoint: ${end} (unverified). Saved approach: A ${recipe.approach_mm.A} mm; B ${recipe.approach_mm.B} mm. Lift/retract: 100 mm. Close 1; open 3; settling ${recipe.settle_s} s. Each fresh path is checked. No per-operation approval pauses.`;
      el('repeat-message').textContent='Recipe reviewed. Check the physical setup, then approve this bounded test.';
    }catch(error){el('repeat-message').textContent=error.message+' No automatic retry.';}
    finally{posting=false;render();}
  });
  el('repeat-start').addEventListener('click',async()=>{
    if(ownerReason()||!recipe||!el('repeat-approved').checked)return;
    const body={...repeatBody('REPEAT'),automatic_test_approved:true,recipe_sha256:recipe.recipe_sha256};
    posting=true;invalidateRecipe();render();
    try{await window.startManualJob('/api/manual-pick-place',body);el('repeat-message').textContent='IPC automatic test accepted. Follow Current operation and use Stop to end the test.';}
    catch(error){el('repeat-message').textContent=error.message;}
    finally{posting=false;render();}
  });
  el('manual-setup').checked=false;
  el('manual-item-state').value='';render();
})();
