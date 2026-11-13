'use strict';
const token=document.querySelector('meta[name="module-token"]').content;
const moduleName=document.body.dataset.module;
const $=id=>document.getElementById(id);
let imageJob=null, imageUrl=null, autoEnabled=false, activeJob=null;
let current=moduleName?localStorage.getItem('ano-job-'+moduleName):null;
let calibrationHandoff=null;
let pendingSend=moduleName?JSON.parse(localStorage.getItem('ano-send-'+moduleName)||'null'):null, submitting=false;
function updateStarts(){
  document.querySelectorAll('[data-action]').forEach(button=>button.disabled=submitting||!!pendingSend||!!activeJob||(button.hasAttribute('data-auto')&&!autoEnabled));
  if($('retry'))$('retry').disabled=submitting;
}
async function api(path,body){
  const opts={headers:{'X-Module-Token':token}};
  if(body!==undefined){opts.method='POST';opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}
  const response=await fetch(path,opts), value=await response.json();
  if(!response.ok){const error=new Error(value.error||'Request failed.');error.status=response.status;throw error;}
  return value;
}
function field(id){return $(id).value.trim();}
function positiveNumber(id,fallback){const value=Number(field(id));return Number.isFinite(value)&&value>0?value:fallback;}
function nonNegativeNumber(id,fallback){const value=Number(field(id));return Number.isFinite(value)&&value>=0?value:fallback;}
function previewCalibration(){document.dispatchEvent(new CustomEvent('preview-calibration-selection',{detail:field('calibration')}));}
function target(){return field('kind')==='charuco'?{kind:'charuco',board_manifest:field('manifest'),board:field('board')}:
  {kind:'aruco',dictionary:field('dictionary'),marker_id:Number(field('marker_id')),marker_length_mm:Number(field('marker_length_mm'))};}
function values(action){
  if(moduleName==='calibration') return {action:action==='verify-capture'?'manual':action,session:action==='verify-capture'?field('verification'):field('session'),
    ...(action==='verify-capture'?{fit_session:field('session')}:{}),manifest:field('manifest'),board:field('board'),serial:field('serial'),robot_ip:field('robot_ip'),
    camera_provider:field('camera_provider'),camera_fps:action==='camera'&&field('camera_provider')==='realsense'?15:Number(field('camera_fps')),camera_exposure_us:positiveNumber('camera_exposure_us',5000),camera_gain_db:nonNegativeNumber('camera_gain_db',0),intrinsics:field('intrinsics'),calibration:['verify','verify-capture','replay'].includes(action)?field('calibration'):'',verification:field('verification'),collision_scene:['plan','collect','replay'].includes(action)?field('collision_scene'):'',
    views:Number(field('views')),tilt_deg:Number(field('tilt_deg')),roll_deg:Number(field('roll_deg'))};
  const project=field('project'), calibration=field('calibration');
  if(action==='simulate') return {action,project,calibration,target:target()};
  if(action!=='configure') return {action,project,marker_unchanged:$('marker_unchanged').checked};
  const tcp=field('tcp').split(/[\s,]+/).map(Number);
  if(tcp.length!==6||!tcp.every(Number.isFinite)) throw Error('Enter six TCP values.');
  return {action,settings:{project,calibration,robot_ip:field('robot_ip'),tcp:tcp.map((n,i)=>i<3?n/1000:n),
    target:target(),motion_model:'planar',approach_mm:Number(field('approach_mm')),tcp_confirmed:$('tcp_confirmed').checked}};
}
function notice(message,error=false){if($('message')){$('message').textContent=message;$('message').className=error?'error':'';}}
async function send(){
  if(!pendingSend||submitting)return;
  submitting=true;updateStarts();
  try{
    const job=await api('/api/'+moduleName,pendingSend);
    activeJob=job.id;current=job.id;localStorage.setItem('ano-job-'+moduleName,current);
    pendingSend=null;localStorage.removeItem('ano-send-'+moduleName);$('retry').hidden=true;
    notice('Request accepted. The Interface PC owns this job.');await refresh();
  }catch(error){if(error.status){pendingSend=null;localStorage.removeItem('ano-send-'+moduleName);$('retry').hidden=true;notice(error.message,true);}else{notice(error.message+' Retry the same request if the connection failed.',true);$('retry').hidden=false;}}
  finally{submitting=false;updateStarts();}
}
async function start(action){
  if(pendingSend){notice('Retry the pending request before starting another.',true);return;}
  if(activeJob)return;
  try{if(moduleName==='calibration'&&['manual','verify-capture','plan','collect','replay'].includes(action)&&![...document.querySelectorAll('.ready')].every(c=>c.checked))throw Error('Complete the setup checks before collecting views.');pendingSend={...values(action),request_id:crypto.randomUUID()};localStorage.setItem('ano-send-'+moduleName,JSON.stringify(pendingSend));await send();}
  catch(error){notice(error.message,true);}
}
async function answer(id,value){
  try{await api('/api/answer',{job_id:current,request_id:id,value});await refresh();}
  catch(error){notice(error.message,true);}
}
function render(job){
  $('job_id').value=job.id;
  $('summary').textContent='Job '+job.id+' · '+job.state+(job.error?' · '+job.error:'');
  if(moduleName==='calibration'){
    $('summary').dataset.state=job.error?'failed':job.state;
    if(job.error)$('summary').textContent='Job '+job.id+' · '+job.state+' · Error: '+job.error;
    const end=Number.isFinite(job.finished)?job.finished:Date.now()/1000;
    const elapsed=Number.isFinite(job.created)?Math.max(0,Math.floor(end-job.created)):null;
    $('job-elapsed').textContent='Elapsed job time: '+(elapsed===null?'unavailable':Math.floor(elapsed/60)+'m '+elapsed%60+'s');
    let problem='';
    const messages=(job.events||[]).flatMap(event=>{
      if(['request','result','verification'].includes(event.type)||(event.type==='observation'&&event.status==='ok'))problem='';
      if(['log','error'].includes(event.type)&&typeof event.message==='string'){
        const lines=event.message.split(/\r?\n/);
        for(const line of lines){
          const text=line.trimStart().toLowerCase();
          if(text.startsWith('rtdecontrolinterface exception:')||text.startsWith('rtdereceiveinterface boost system exception:')||text.startsWith('rtdecontrolinterface: could not receive data'))problem='Robot interface error reported\n'+line;
          else if(line.trimStart().startsWith('error:'))problem='Native operation error reported\n'+line;
        }
        return lines;
      }
      if(event.type==='observation')return [(event.label||'Observation')+': '+event.status+' · '+(event.corners??'—')+' corners'+((event.reasons||[]).length?' · '+event.reasons.join(', '):'')];
      return [];
    }).filter(line=>line.trim()).slice(-8).join('\n')||'No messages received.';
    $('job-attention').textContent=problem;
    $('job-attention').hidden=!problem||job.state==='done';
    if($('job-log').textContent!==messages){$('job-log').textContent=messages;$('job-log').scrollTop=$('job-log').scrollHeight;}
    const result=[...(job.events||[])].reverse().find(e=>e.type==='result');
    const intrinsic=[...(job.events||[])].reverse().find(e=>e.type==='intrinsics');
    const obs=[...(job.events||[])].reverse().find(e=>e.type==='observation');
    const verification=[...(job.events||[])].reverse().find(e=>e.type==='verification');
    for(const [event,id] of [[result,'calibration'],[intrinsic,'intrinsics']]) {
      if(!event?.path||(id==='calibration'&&job.state!=='done'))continue;
      const key='ano-applied-'+id, identity=job.id+':'+event.path;
      if(localStorage.getItem(key)!==identity){$(id).value=event.path;localStorage.setItem(key,identity);saveCalibrationForm();if(id==='calibration')previewCalibration();}
    }
    const completed=job.state==='done'&&job.module==='calibration'?job.result:null;
    calibrationHandoff=completed?.path&&completed.result?.type==='eye_in_hand'&&['good','acceptable'].includes(completed.result.verdict)?completed:null;
    $('continue-teaching').disabled=!calibrationHandoff||field('calibration')!==calibrationHandoff.path;
    $('calibration-handoff').textContent=calibrationHandoff?(calibrationHandoff.result.simulation_truth||calibrationHandoff.result.camera?.simulated||calibrationHandoff.result.robot?.host==='simulated'?'Simulated calibration. This does not qualify hardware.':'Review the selected calibration before teaching.'):'Complete a usable calibration first.';
    if(result?.result){
      const r=result.result,m=r.metrics||{},c=m.consistency||{};
      const fmt=value=>typeof value==='number'?value.toFixed(3):'—';
      $('summary').textContent+=(r.simulation_truth||r.camera?.simulated||r.robot?.host==='simulated'?' · SIMULATION':'')+' · '+(r.verdict||'Result')+'\nSamples: '+(m.sample_count??'—')+
        '\nReprojection RMSE: '+fmt(m.reprojection_rmse_px)+' px'+
        '\nConsistency maximum: '+fmt(c.position_max_mm)+' mm / '+fmt(c.rotation_max_deg)+' degrees'+
        ((r.warnings||[]).length?'\nWarnings: '+r.warnings.join('; '):'');
    }
    if(obs){const observations=(job.events||[]).filter(e=>e.type==='observation');$('summary').textContent+=' · '+obs.status+' · '+obs.corners+' corners · '+observations.filter(e=>e.status==='ok').length+' accepted / '+observations.filter(e=>e.status!=='ok').length+' rejected'+(obs.rmse_px!=null?' · RMSE '+obs.rmse_px.toFixed(3)+' px':'')+' · '+(obs.reasons||[]).join(', ');}
    if(job.result?.reasons)$('summary').textContent+=' · '+job.result.reasons.join(', ');
    if(job.result?.preview_only)$('summary').textContent+=' · preview only (no pose)';
    const seenCamera=job.result?.camera;
    if(seenCamera&&Number.isFinite(seenCamera.exposure_time_us))$('summary').textContent+=' · '+Math.round(seenCamera.exposure_time_us)+' µs'+(Number.isFinite(seenCamera.gain_db)?' · '+seenCamera.gain_db.toFixed(2)+' dB':'');
    if(verification?.report)$('summary').textContent+=' · verification '+(verification.report.passed?'PASS':'FAIL');
  }
  const clean=JSON.parse(JSON.stringify(job));
  for(const event of clean.events||[]) if(event.image)event.image='[image shown above]';
  if(clean.result?.image_jpeg_base64)clean.result.image_jpeg_base64='[image shown above]';
  $('job').textContent=JSON.stringify(clean,null,2);
  const event=[...(job.events||[])].reverse().find(e=>e.image);
  const image=job.result?.image_jpeg_base64||event?.image;
  if(image){$('preview').src='data:image/jpeg;base64,'+image;$('preview').hidden=false;}
  else if(moduleName==='teaching'&&job.state==='done'&&(typeof job.result?.project==='string'||job.result?.project?.project)){
    if(imageJob!==job.id){
      imageJob=job.id;
      fetch('/api/teaching-image?project='+encodeURIComponent(typeof job.result.project==='string'?job.result.project:job.result.project.project),{headers:{'X-Module-Token':token}})
        .then(r=>{if(!r.ok)throw Error('No image');return r.blob();})
        .then(blob=>{if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=URL.createObjectURL(blob);$('preview').src=imageUrl;$('preview').hidden=false;})
        .catch(()=>{$('preview').hidden=true;});
    }
  }else {$('preview').hidden=true;imageJob=null;}
  const panel=$('pending');panel.replaceChildren();
  if(job.pending){
    if(job.module==='calibration'&&job.auto_scene){const scope=Object.prototype.hasOwnProperty.call(job.auto_scene,'collision_scope')?job.auto_scene.collision_scope:'cell',line=document.createElement('p');line.id='accepted-scene-scope';line.textContent=scope==='supervised_self'?'Accepted job: arm and attached tools only. External objects are not checked.':scope==='cell'?'Accepted job: cell collision scope.':'Accepted job: unknown collision scope. Check job details.';panel.append(line);}
    const p=document.createElement('p');p.textContent='Operator request: '+job.pending.summary;panel.append(p);
    const allowed=autoEnabled&&activeJob===job.id&&job.module==='calibration'&&['collect','replay'].includes(job.auto_action);
    const options=job.pending.kind==='manual'?(job.auto_action?[['Cancel job','q']]:[['Capture view',''],['Finish collection','q']]):
      job.pending.kind==='motion'?(allowed?[['Approve phase',true],['Cancel phase',false]]:[['Cancel phase',false]]):[];
    for(const [label,value] of options){
      const button=document.createElement('button');button.textContent=label;button.onclick=()=>answer(job.pending.id,value);panel.append(button);
    }
  }
}
async function refresh(){
  const observedActive=activeJob;
  try{
    const status=await api('/api/status');
    autoEnabled=status.allow_auto_calibration===true&&status.robot_model?.valid!==false;if(activeJob===observedActive)activeJob=status.owner.active;
    if($('model-status')){const model=status.robot_model;$('model-status').textContent=model?(model.valid?'Robot model: '+(model.nominal?'nominal':'selected')+' · SHA-256 '+model.sha256:'Robot model changed or unavailable.'): 'Robot model status unavailable.';}
    updateStarts();
    if($('auto-capability'))$('auto-capability').textContent=autoEnabled?'Scene-gated automatic calibration is enabled.':'Automatic calibration is unavailable.';
    if(current&&$('job')) render(await api('/api/jobs/'+encodeURIComponent(current)));
    $('status').textContent=status.owner.active?'Connected · active job '+status.owner.active:'Connected · no active job';
    if($('message').textContent==='Connecting…'||$('message').textContent.startsWith('Connection lost. The Interface PC retains accepted jobs. '))notice('Connected.');
  }catch(error){autoEnabled=false;updateStarts();
    $('status').textContent='Disconnected.';
    if(moduleName==='calibration'){
      $('summary').dataset.state='disconnected';$('summary').textContent='Disconnected. The last job status is not current.';
      $('job-elapsed').textContent='Elapsed job time: unavailable while disconnected.';$('job-attention').hidden=true;
    }
    notice('Connection lost. The Interface PC retains accepted jobs. '+error.message,true);}
}
const calibrationFields=['session','manifest','board','serial','robot_ip','camera_provider','camera_fps','camera_exposure_us','camera_gain_db','intrinsics','calibration','verification','collision_scene','views','tilt_deg','roll_deg'];
function saveCalibrationForm(){
  if(moduleName==='calibration')localStorage.setItem('ano-calibration-form',JSON.stringify(Object.fromEntries(calibrationFields.map(id=>[id,$(id).value]))));
}
function syncBaslerFields(){
  const basler=field('camera_provider')==='basler';
  $('basler-exposure-field').hidden=!basler;
  $('basler-gain-field').hidden=!basler;
  if(basler){
    if(!field('camera_exposure_us'))$('camera_exposure_us').value='5000';
    if(!field('camera_gain_db'))$('camera_gain_db').value='0';
  }
}
if(moduleName==='calibration'){
  const saved=JSON.parse(localStorage.getItem('ano-calibration-form')||'{}');
  for(const id of calibrationFields){if(typeof saved[id]==='string')$(id).value=saved[id];$(id).addEventListener('input',saveCalibrationForm);}
  syncBaslerFields();

  $('camera_provider').addEventListener('change',()=>{$('camera_fps').value=field('camera_provider')==='basler'?'20':'30';syncBaslerFields();saveCalibrationForm();});
  $('collision_scene').addEventListener('input',()=>{$('plan-scene-scope').textContent='Scene scope is not loaded.';document.dispatchEvent(new CustomEvent('calibration-scene-select',{detail:field('collision_scene')}));});
  document.addEventListener('calibration-scene-scope',event=>{if(event.detail?.selection!==field('collision_scene'))return;$('plan-scene-scope').textContent=event.detail.scope==='supervised_self'?'Selected scene: arm and attached tools only. External objects are not checked.':'Selected scene: cell scope. Check external objects and cell coverage.';});
  document.addEventListener('calibration-scene-saved',event=>{if(typeof event.detail!=='string'||!event.detail.startsWith('/'))return;$('collision_scene').value=event.detail;saveCalibrationForm();});
  $('calibration').addEventListener('input',()=>{$('continue-teaching').disabled=!calibrationHandoff||field('calibration')!==calibrationHandoff.path;previewCalibration();});
  $('continue-teaching').addEventListener('click',()=>{if(!calibrationHandoff||field('calibration')!==calibrationHandoff.path)return;const r=calibrationHandoff.result;localStorage.setItem('ano-handoff-teaching',JSON.stringify({calibration:calibrationHandoff.path,robot_ip:r.robot?.host==='simulated'?'':(r.robot?.host||''),serial:r.camera?.device?.serial_number||'',camera_provider:r.camera?.provider||'realsense',camera_fps:r.camera?.stream?.fps,camera_exposure_time_us:r.camera?.exposure_time_us,simulation:!!(r.simulation_truth||r.camera?.simulated||r.robot?.host==='simulated')}));location.href='/teaching';});
  let toolViewNeedsFit=true;
  document.querySelectorAll('[data-step]').forEach(button=>button.onclick=()=>{
    document.querySelectorAll('[data-page]').forEach(page=>page.hidden=page.dataset.page!==button.dataset.step);
    document.querySelectorAll('[data-step]').forEach(item=>item.setAttribute('aria-current',item===button?'step':'false'));
    if(button.dataset.step==='1'&&toolViewNeedsFit){toolViewNeedsFit=false;requestAnimationFrame(()=>$('fit-view').click());}
  });
  document.querySelector('[data-step="0"]').click();
}
if(moduleName==='pick-place'){
  const fields=['project','calibration','manifest'], saved=JSON.parse(localStorage.getItem('ano-pick-place-form')||'{}'), handoff=JSON.parse(localStorage.getItem('ano-handoff-pick-place')||'null');
  localStorage.removeItem('ano-handoff-pick-place');
  // Legacy simulation requests and results have no operator controls on this page.
  pendingSend=null;current=null;
  for(const id of fields){
    if(typeof saved[id]==='string')$(id).value=saved[id];
    if(handoff&&typeof handoff[id]==='string')$(id).value=handoff[id];
    $(id).addEventListener('input',()=>{
      $('pick-provenance').textContent='Inputs changed. Review the saved project, calibration and marker provenance.';
      localStorage.setItem('ano-pick-place-form',JSON.stringify(Object.fromEntries(fields.map(key=>[key,$(key).value]))));
    });
  }
  $('pick-provenance').textContent=handoff?.simulation?'Selected inputs were simulated; choose physically taught data for real movement.':'Review the selected teaching project, calibration and marker provenance.';
  window.applyTestProfile();
}
if(moduleName&&moduleName!=='pick-place'){
  const retry=document.createElement('button');retry.id='retry';retry.textContent='Retry the same request';retry.hidden=!pendingSend;
  $('current-operation').querySelector('.operation-body').prepend(retry);retry.onclick=send;updateStarts();
  document.querySelectorAll('[data-action]').forEach(button=>button.onclick=()=>start(button.dataset.action));
  $('load').onclick=()=>{current=field('job_id');localStorage.setItem('ano-job-'+moduleName,current);refresh();};
}
refresh();setInterval(refresh,1500);
