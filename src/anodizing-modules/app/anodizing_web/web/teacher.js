'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="teach-token"]').content;
let defaults, state = {version:'0.3.1',busy:null,error:'',completed:null,failed:null,project:null}, dirty = false, posting = false, previousResult = '', imageVersion = 0;
let minPixelsPerModule = 5;
let connectionOK = true, validationImageId = '';
let initialEdits = new Set();
const steps = ['configure', 'capture', 'teach', 'locate', 'validate'];
let activeStep = 0;
const stepNames = ['Set up', 'Capture marker', 'Teach pick', 'Read & export', 'Validation'];
const guidance = [
  ['Prepare your setup', 'Use the calibration for this camera and its unchanged wrist mounting. Confirm the gripper TCP against the pendant.'],
  ['Frame the whole marker', 'Keep the camera and board still during capture. Review the detected axes and quality before continuing.'],
  ['Keep the marker fixed', 'Only move the robot. Teach the final pick position and orientation, then stop before saving.'],
  ['Read from a clear view', 'Show the whole marker and stop. The observation pose may differ from teaching. Re-read for every pick.'],
  ['Test marker tracking', 'Review the capture, endpoint, workspace and scene. Clear the physical area before one checked moveJ. Destination Z is capture height; actual TCP height varies along the joint trajectory.']
];
function availableStep() {
  const f=state?.project?.flow||{};
  // Validation makes its own fresh capture. Reopening clears export freshness,
  // but the saved marker-to-pick relationship is sufficient to capture again.
  return dirty||!state?.project ? 0 : f.taught ? 4 : f.captured ? 2 : 1;
}
function showStep(index) {
  if(state?.busy||posting||index<0||index>availableStep()) return;
  activeStep=index; render();
  $('title-'+steps[index]).focus({preventScroll:true});
}
function renderWizard(ready,busy) {
  if(activeStep>availableStep()) activeStep=availableStep();
  steps.forEach((step,index)=>{
    $('step-'+step).hidden=index!==activeStep;
    const nav=$('nav-'+step), light=$('light-'+step);
    nav.disabled=busy||index>availableStep();
    nav.setAttribute('aria-current',index===activeStep?'step':'false');
    nav.classList.toggle('done',!dirty&&ready[step]);
    nav.classList.toggle('failed',failedStep()===step&&!!state.error||step==='validate'&&state.project?.validation?.visual_result==='misaligned');
    $('nav-state-'+step).textContent=light.textContent.replace('● ','');
  });
  $('step-counter').textContent=`Step ${activeStep+1} of 5`;
  $('back').disabled=busy||activeStep===0;
  $('next').hidden=activeStep>=4;
  $('next').disabled=busy||activeStep>=availableStep();
  $('next').textContent='Next: '+(stepNames[activeStep+1]||'')+' →';
  $('guide-title').textContent=guidance[activeStep][0];
  $('guide-text').textContent=guidance[activeStep][1];
  $('setup-details').hidden=!state.project||dirty;
  $('camera-placeholder').hidden=!$('capture-image').hidden;
}
function message(text, error=false) { $('message').textContent=text; $('message').classList.toggle('error',error); }
function targetFields() {
  for (const kind of ['charuco','aruco']) document.querySelectorAll('.'+kind).forEach(el=>el.hidden=$('kind').value!==kind);
  const index=(defaults?.picking_boards||[]).findIndex(b=>b.target.board_manifest===$('manifest').value&&b.target.board===$('board').value);
  $('picking-board').value=index<0?'':String(index);
}
function tcpText(tcp) {
  return tcp.map((v,i)=>Number((i<3?v*1000:v).toPrecision(12))).join(', ');
}
function showCalibrationTCP() {
  const calibration=defaults.calibrations.find(c=>c.path===$('calibration').value);
  $('calibration-tcp').textContent=calibration
    ? 'Calibration TCP (XYZ mm, rotation-vector rad): '+tcpText(calibration.tcp)
    : 'This TCP must match the TCP recorded in your selected calibration.';
}
function setField(id,value,property='value') { $(id)[property]=value; }
function fill(s,put=setField) {
  for (const id of ['project','calibration','robot_ip','motion_model','approach_mm']) put(id,s[id]);
  put('max_shift_mm',s.limits?.max_shift_mm??100); put('max_rotation_deg',s.limits?.max_rotation_deg??180);
  put('camera_provider',s.camera_provider||'realsense');
  minPixelsPerModule=s.min_pixels_per_module??5;
  put('refine_before_pick',s.refine_before_pick??false,'checked');
  put('tcp',tcpText(s.tcp)); put('kind',s.target.kind);
  if(s.target.kind==='charuco') { put('manifest',s.target.board_manifest); put('board',s.target.board); }
  else { for(const id of ['dictionary','marker_id','marker_length_mm']) put(id,s.target[id]); }
  targetFields(); showCalibrationTCP();
}
function settings() {
  const kind=$('kind').value;
  const tcp=$('tcp').value.trim().split(/[\s,]+/).map((v,i)=>i<3?Number(v)/1000:Number(v));
  if(tcp.length!==6||!tcp.every(Number.isFinite))
    throw new Error('Active gripper TCP needs six numbers: X, Y, Z in mm, then RX, RY, RZ in rotation-vector radians.');
  return {project:$('project').value,calibration:$('calibration').value,robot_ip:$('robot_ip').value,
    camera_provider:$('camera_provider').value,refine_before_pick:$('refine_before_pick').checked,
    min_pixels_per_module:minPixelsPerModule,
    motion_model:$('motion_model').value,approach_mm:Number($('approach_mm').value),
    tcp,tcp_confirmed:$('tcp_confirmed').checked,
    limits:{max_shift_mm:Number($('max_shift_mm').value),max_rotation_deg:Number($('max_rotation_deg').value),max_tilt_change_deg:10,max_height_change_mm:10,max_tool_tilt_deg:20},
    target:kind==='charuco'?{kind,board_manifest:$('manifest').value,board:$('board').value}:
      {kind,dictionary:$('dictionary').value,marker_id:Number($('marker_id').value),marker_length_mm:Number($('marker_length_mm').value)}};
}
let teacherJob=localStorage.getItem('ano-job-teaching');
let teacherPending=JSON.parse(localStorage.getItem('ano-send-teaching')||'null');
let teacherState=JSON.parse(localStorage.getItem('ano-teacher-state')||'null');
let captureURL=null, simulatedProject=null;
function nativeState(value) {
  return value && typeof value==='object' && value.project && typeof value.project==='object'
    && value.project.settings && value.project.flow;
}
function recoverOldJob() {
  teacherJob=null;teacherState=null;
  localStorage.removeItem('ano-job-teaching');localStorage.removeItem('ano-teacher-state');
  return {version:'0.3.1',busy:null,completed:null,failed:'configure',project:null,
    error:'The previous job is unavailable or simulated. Open a saved teaching project to continue.'};
}
async function moduleRequest(path,body) {
  const headers={'X-Module-Token':token};
  if(body!==undefined) headers['Content-Type']='application/json';
  const response=await fetch(path,{headers,...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
  const value=await response.json();
  if(!response.ok){const error=new Error(value.error||'Request failed');error.status=response.status;throw error;}
  return value;
}
async function sendTeacher() {
  if(!teacherPending) return;
  try {
    const job=await moduleRequest('/api/teaching',teacherPending);
    teacherJob=job.id;localStorage.setItem('ano-job-teaching',teacherJob);localStorage.setItem('ano-teacher-action',teacherPending.action);
    teacherPending=null;localStorage.removeItem('ano-send-teaching');$('retry-teacher').hidden=true;
  } catch(error) {
    if(error.status){teacherPending=null;localStorage.removeItem('ano-send-teaching');$('retry-teacher').hidden=true;}
    else $('retry-teacher').hidden=false;
    throw error;
  }
}
async function request(path,body) {
  if(path==='/api/defaults') return moduleRequest('/api/teacher-defaults');
  if(path==='/api/open') return moduleRequest('/api/teacher-project?project='+encodeURIComponent(body.project));
  if(path==='/api/state') {
    if(!teacherJob) return teacherState?recoverOldJob():{version:'0.3.1',busy:null,error:'',completed:null,failed:null,project:null};
    let job;
    try {job=await moduleRequest('/api/jobs/'+encodeURIComponent(teacherJob));}
    catch(error){if(error.status===404)return recoverOldJob();throw error;}
    if(job.module!=='teaching')return recoverOldJob();
    if(job.state==='done'&&job.result?.simulation===true&&typeof job.result.project==='string'){
      if(simulatedProject?.job!==job.id){const settings=await moduleRequest('/api/teacher-project?project='+encodeURIComponent(job.result.project));simulatedProject={job:job.id,settings};}
      return {version:'0.3.1',busy:null,error:null,completed:null,failed:null,project:null};
    }
    simulatedProject=null;
    let current;
    try {current=await moduleRequest('/api/teacher-state?job='+encodeURIComponent(teacherJob));}
    catch(error){
      if(error.status===404&&job.state==='done') {
        const manual=await window.inspectActiveManualJob();
        if(manual){
          const prior=nativeState(job.result)?job.result.project:nativeState(teacherState)?teacherState.project:null;
          return {version:'0.3.1',busy:'manual_handoff',completed:null,failed:null,error:null,
            motion:{},motion_available:false,requires_setup:true,project:prior,
            manual_handoff:manual.id,job_state:job.state};
        }
        if(teacherState?.requires_setup){dirty=true;return {...teacherState,job_state:job.state};}
        const prior=nativeState(teacherState)?teacherState.project:nativeState(job.result)?job.result.project:null;
        dirty=true;teacherPending=null;simulatedProject=null;
        localStorage.removeItem('ano-send-teaching');$('retry-teacher').hidden=true;
        for(const id of ['tcp_confirmed','marker_unchanged','validation-confirmed','observation-return-confirmed']) $(id).checked=false;
        $('capture-image').hidden=true;
        teacherState={version:'0.3.1',busy:null,completed:null,failed:'configure',motion:{},motion_available:false,
          requires_setup:true,project:prior?{project:prior.project,settings:prior.settings,flow:{}}:null,
          error:'Teacher session expired after restart. Open a saved teaching project or complete setup again.'};
        localStorage.setItem('ano-teacher-state',JSON.stringify(teacherState));
        return {...teacherState,job_state:job.state};
      }
      if(error.status===404&&['failed','interrupted'].includes(job.state)) {
        const prior=nativeState(teacherState)?teacherState:{version:'0.3.1',project:null};
        return {...prior,busy:null,motion:{},motion_available:false,job_state:job.state,
          error:job.error||'Teacher session interrupted. Open the project and capture again.',failed:localStorage.getItem('ano-teacher-action')||'configure'};
      }
      throw error;
    }
    if(!nativeState(current))return recoverOldJob();
    teacherState=current;localStorage.setItem('ano-teacher-state',JSON.stringify(current));
    return {...current,job_state:job.state,
      error:['failed','interrupted'].includes(job.state)?job.error||current.error||'Job interrupted.':current.error,
      failed:['failed','interrupted'].includes(job.state)?localStorage.getItem('ano-teacher-action')||current.failed:current.failed};
  }
  if(path==='/api/validation/heartbeat'||path==='/api/validation/stop')
    return moduleRequest('/api/teacher-control',{job_id:teacherJob,action:path.endsWith('/stop')?'stop':'heartbeat'});
  const action=({'validation/capture':'capture_validation','validation/move':'validate_move','validation/review':'review_validation','validation/return':'return_observation'})[path.substring('/api/'.length)]||path.substring('/api/'.length);
  if(!['configure','capture','teach','locate','capture_validation','validate_move','review_validation','return_observation'].includes(action)) throw new Error('This Teacher action is unavailable.');
  if(state.requires_setup&&action!=='configure') throw new Error('Teacher session expired. Open the project and complete setup again.');
  if(teacherPending) throw new Error('Retry the pending request first.');
  teacherPending={request_id:crypto.randomUUID(),action,...(action==='configure'?{settings:body}:{project:$('project').value,...body})};
  localStorage.setItem('ano-send-teaching',JSON.stringify(teacherPending));
  await sendTeacher();
  return {accepted:true};
}
async function loadTeacherFile(name) {
  if(!teacherJob) throw new Error('Select a teaching job first.');
  const response=await fetch('/api/teacher-file?job='+encodeURIComponent(teacherJob)+'&name='+encodeURIComponent(name),{headers:{'X-Module-Token':token}});
  if(!response.ok) throw new Error('File is unavailable.');
  return response.blob();
}
let validationURL=null;
async function loadValidationImage(id) {
  const response=await fetch('/api/teaching-image?project='+encodeURIComponent(state.project.project)+'&kind=validation&capture_id='+encodeURIComponent(id),{headers:{'X-Module-Token':token}});
  if(!response.ok)throw new Error('Validation image is unavailable.');
  const blob=await response.blob();
  if(state?.project?.validation?.id!==id)return;
  if(validationURL)URL.revokeObjectURL(validationURL);
  validationURL=URL.createObjectURL(blob);$('validation-image').src=validationURL;$('validation-image').hidden=false;$('validation-placeholder').hidden=true;
}
async function loadCapture() {
  if(!state?.project?.project)return;
  const response=await fetch('/api/teaching-image?project='+encodeURIComponent(state.project.project),{headers:{'X-Module-Token':token}});
  if(!response.ok) throw new Error('Image is unavailable.');
  const blob=await response.blob();if(captureURL)URL.revokeObjectURL(captureURL);
  captureURL=URL.createObjectURL(blob);$('capture-image').src=captureURL;$('capture-image').hidden=false;
}
function restoreCapture() {
  if(state?.project?.flow.captured) {
    loadCapture().catch(()=>{$('capture-image').hidden=true;});
  }
}
function poseText(pose) { return pose ? '['+pose.map(v=>v.toFixed(6)).join(', ')+']' : ''; }
function failedStep() {
  if(state?.failed==='return_observation') return activeStep===2?'teach':'validate';
  return ['capture_validation','validate_move'].includes(state?.failed)?'validate':state?.failed;
}
function validationPose(pose) { return pose ? pose.map((v,i)=>`${['X','Y','Z','RX','RY','RZ'][i]}: ${(i<3?v*1000:v).toFixed(i<3?2:5)} ${i<3?'mm':'rad'}`).join('\n') : ''; }
function renderValidation(p,busy) {
  const v=dirty?null:p?.validation;
  const fresh=v?.validation_mode==='fixed_z'&&v?.status==='captured'&&Date.now()/1000-v.timestamp>=0&&Date.now()/1000-v.timestamp<=60;
  const supported=p?.settings.motion_model==='planar'&&state.motion_available===true;
  const contract='One checked, unblended moveJ: destination XY and orientation follow the taught marker; destination Z is capture height. Actual TCP height varies along the joint trajectory. No moveL, descent, gripper or pick/place action. Keep this tab active; use Stop or the pendant if needed.';
  $('validation-instructions').textContent=contract;
  $('validation-target-title').textContent='Joint-test destination · Z at capture height';
  $('validation-move').textContent='Run checked joint test';
  $('validation-confirmation-text').textContent='The AGV and marker/object stayed fixed. I reviewed the endpoint, workspace and selected scene. The physical area is clear for joint movement, including changing TCP height. External objects require operator supervision.';
  $('validation-motion-help').textContent=contract+' Each attempt needs a fresh capture within 60 seconds. '+(supported?'':state.motion_unavailable_reason||'Only planar projects support this test.');
  $('validation-review-help').textContent='Robot arrival does not establish camera accuracy. Independently check alignment; this test does not validate pick Z.';
  $('validation-collision-scope').textContent='Collision scope: '+(state.collision_scope||'select an explicit scene')+'. supervised_self checks the robot itself; external objects are not checked.';
  const motion=state.motion||{};
  const motionText=motion.error||motion.message||state.error||(state.busy?String(motion.pending||state.busy):v?.status||'Idle');
  $('teacher-motion-status').textContent=motionText;
  $('teacher-motion-status').classList.toggle('error',!!(motion.error||state.error));
  $('validation-stop').hidden=!(['validate_move','return_observation'].includes(state.busy)||motion.moving||motion.pending);
  $('validation-stop').disabled=!connectionOK||!teacherJob;
  const commonReason=busy?'Wait for the current operation.':dirty||state.requires_setup||!p?'Complete setup first.':!connectionOK?'Reconnect Teacher.':!state.motion_available?(state.motion_unavailable_reason||'Checked movement is unavailable.'):!$('validation-collision-scene').value.trim()?'Select a reviewed collision scene.':'';
  const returnReason=commonReason||(!p?.observation?'Capture the teaching reference in Step 2.':!$('observation-return-confirmed').checked?'Review and confirm the return joint path.':!$('observation-return-speed').checkValidity()?'Set return speed from 1 to 6 degrees/s.':'');
  const validationReason=commonReason||(p?.settings.motion_model!=='planar'?'Only planar projects support this test.':!fresh?(v?.status==='captured'?'Validation capture expired or unsupported. Capture again.':'Capture a fresh validation marker.'):!$('validation-confirmed').checked?'Review and confirm the validation joint path.':!$('validation-speed').checkValidity()?'Set joint speed from 1 to 6 degrees/s.':'');
  $('observation-return-reason').textContent=returnReason;
  $('validation-move-reason').textContent=validationReason;
  const canReturn=!busy&&!dirty&&connectionOK&&!!p?.observation;
  $('observation-return').disabled=!!returnReason;
  $('observation-return-confirmed').disabled=busy||!canReturn;
  $('teach-observation-return').disabled=true;
  $('observation-return-speed').disabled=busy;
  const returned=state.return_result;
  const returnText=state.busy==='return_observation'?'Returning to the saved observation joints…':returned
    ? returned.status+(returned.message?' · '+returned.message:'')+(returned.joint_error_deg!==undefined?` · maximum joint error ${returned.joint_error_deg.toFixed(3)}°`:'')
    : '';
  for(const id of ['observation-return-result','teach-observation-return-result']) $(id).textContent=returnText;
  $('observation-return-actual-pose').textContent=returned?.actual_tcp_pose_m_rad?'Measured TCP:\n'+validationPose(returned.actual_tcp_pose_m_rad)+'\nActual joints (degrees): '+returned.actual_joints_rad.map(q=>(q*180/Math.PI).toFixed(3)).join(', '):'';
  $('validation-capture').disabled=busy||dirty||!connectionOK||!p?.flow.taught||p?.settings.motion_model!=='planar';
  $('validation-move').disabled=!!validationReason;
  $('validation-confirmed').disabled=busy||!fresh;
  for(const id of ['validation-speed','validation-collision-scene','validation-workspace-min','validation-workspace-max']) $(id).disabled=busy;
  $('validation-preview').hidden=!v;
  $('validation-result').hidden=!v||v.status==='captured'||v.status==='moving';
  $('validation-aligned').disabled=$('validation-misaligned').disabled=busy||v?.status!=='completed';
  $('validation-observe-joints').textContent=p?.observation?'Observation joints (degrees): '+p.observe_joints.map(q=>(q*180/Math.PI).toFixed(2)).join(', '):'';
  if(v) {
    $('validation-marker-pose').textContent=validationPose(v.marker_tcp_pose_m_rad);
    $('validation-pick-pose').textContent=validationPose(v.pick_tcp_pose_m_rad);
    $('validation-target-pose').textContent=validationPose(v.validation_tcp_pose_m_rad);
    const r=v.validation;
    $('validation-change').textContent=`Pick shift: ${r.shift_from_taught_mm.toFixed(2)} mm\nPick rotation: ${r.rotation_from_taught_deg.toFixed(2)}°\nMarker yaw: ${r.marker_yaw_change_deg.toFixed(2)}°`;
    if(r.marker_height_change_mm!==undefined) $('validation-change').textContent+=`\nMarker Z change: ${r.marker_height_change_mm.toFixed(2)} mm\nMarker tilt change: ${r.marker_tilt_change_deg.toFixed(2)}°`;
    $('validation-quality').textContent=`${v.quality.frames_accepted} accepted frames · fit ${v.quality.reprojection_rmse_px.toFixed(3)} px`;
    $('validation-freshness').textContent=v.status==='captured'?(fresh?`Ready for one attempt · ${Math.max(0,Math.floor(60-(Date.now()/1000-v.timestamp)))} seconds remaining`:'Capture expired. Capture again before motion.'):'Capture used · capture again for another motion test.';
    $('validation-motion-result').textContent=v.status+(v.message?' · '+v.message:'')+(v.error?`\nTCP error: ${v.error.position_mm.toFixed(2)} mm / ${v.error.rotation_deg.toFixed(3)}°`:'')+(v.visual_result?'\nVisual alignment: '+v.visual_result:'');
    $('validation-actual-pose').textContent=v.actual_tcp_pose_m_rad?'Measured TCP:\n'+validationPose(v.actual_tcp_pose_m_rad):'';
    if(validationImageId!==v.id) {
      validationImageId=v.id; $('validation-confirmed').checked=false; $('validation-measured-error').value='';
      $('validation-move').disabled=true;
      loadValidationImage(v.id).catch(()=>{$('validation-image').hidden=true;$('validation-placeholder').hidden=false;});
    }
  } else {
    $('validation-image').hidden=true; $('validation-confirmed').checked=false; validationImageId='';
  }
  $('validation-placeholder').hidden=!$('validation-image').hidden;
  $('validation-history').textContent=(!dirty&&p?.validation_history?.length)?p.validation_history.slice().reverse().map((t,i)=>`${p.validation_history.length-i}. ${new Date(t.timestamp*1000).toLocaleString()} · ${t.status}${t.error?` · ${t.error.position_mm.toFixed(2)} mm / ${t.error.rotation_deg.toFixed(3)}°`:''} · visual: ${t.visual_result||'unreviewed'}${t.measured_position_error_mm!==undefined?` · independently measured: ${t.measured_position_error_mm.toFixed(2)} mm (${t.within_2mm?'within':'outside'} 2 mm)`:''}${t.message?'\n'+t.message:''}`).join('\n'):'No motion tests yet.';
}
function render() {
  if(!state) return;
  $('version').textContent='v'+state.version;
  const p=state.project, f=p?.flow||{}, busy=!!state.busy||posting;
  const ready={configure:!!p,capture:!!f.captured,teach:!!f.taught,locate:!!f.exported,validate:p?.validation?.status==='completed'&&p.validation.visual_result==='aligned'};
  for(const step of steps) {
    const light=$('light-'+step); let status=dirty?'pending':ready[step]?'ready':'pending';
    if(state.busy===step||step==='validate'&&['capture_validation','validate_move'].includes(state.busy)
        ||state.busy==='return_observation'&&step===(activeStep===2?'teach':'validate')) status='running';
    if(state.error&&failedStep()===step) status='failed';
    if(step==='validate'&&p?.validation?.visual_result==='misaligned') status='failed';
    light.className='light '+status; light.textContent='● '+({pending:'Pending',ready:'Ready',running:'Running',failed:'Failed'}[status]);
  }
  $('configure').disabled=busy; $('open').disabled=busy;
  for(const input of $('setup').querySelectorAll('input,select')) input.disabled=busy;
  $('capture').disabled=busy||dirty||!p;
  $('teach').disabled=busy||dirty||!f.captured||!$('marker_unchanged').checked;
  $('locate').disabled=busy||dirty||!f.taught;
  $('continue-pick-place').disabled=busy||dirty||(!f.taught&&!simulatedProject);
  $('project-handoff').textContent=simulatedProject?'Simulated teaching data cannot qualify real movement. Select a physically taught project.':f.taught?'Open Manual PICK / PLACE to review the saved inputs and physical declarations.':'Complete teaching before continuing to Manual PICK / PLACE.';
  $('marker_unchanged').disabled=busy||!f.captured;
  $('export-result').hidden=dirty||!f.exported; $('saved-links').hidden=dirty||!f.taught;
  renderValidation(p,busy);
  renderWizard(ready,busy);
  if(p&&!dirty&&!state.requires_setup) {
    $('movement-limits').textContent=`Configured limits: ${p.settings.limits.max_shift_mm} mm target shift and ${p.settings.limits.max_rotation_deg}° rotation.`;
    $('setup-result').textContent='Calibration '+p.calibration.verdict.toUpperCase()+' · '+p.target+'\nCamera serial: '+(p.calibration.camera_serial||'not recorded')+'\nMinimum marker resolution: '+Math.max(4,p.settings.min_pixels_per_module)+' px/module for teaching; '+p.settings.min_pixels_per_module+' px/module for later reads\n'+p.project;
    const o=p.observation;
    $('capture-result').textContent=o?`${o.quality.frames_accepted} accepted frames · ${o.quality.corner_count} corners · fit ${o.quality.reprojection_rmse_px.toFixed(3)} px` : '';
    $('observe-joints').textContent=o?'Saved observation joints (degrees): '+p.observe_joints.map(q=>(q*180/Math.PI).toFixed(2)).join(', '):'Saved observation joints appear after capture.';
    $('teach-result').textContent=f.taught?'Saved gripper TCP endpoint (base m/rad):\n'+poseText(p.taught.taught_tcp_in_base)+'\nMarker-relative relationship saved in grasp_pick.json.':'';
    $('storage').textContent='Project files: '+p.project;
    if(p.exported) { $('pick-pose').textContent=poseText(p.exported.pick_tcp_pose_m_rad)+(p.exported.refinement_required?'\nPreview only · use live vision to refine before picking':''); $('approach-pose').textContent=poseText(p.exported.approach_tcp_pose_m_rad); }
  }
}
async function post(action,body) {
  $('observation-return-confirmed').checked=false;
  posting=true;
  try {
    previousResult='';
    render();
    if(['validation/move','validation/return'].includes(action)) await request('/api/validation/heartbeat',{});
    await request('/api/'+action,body); dirty=false;
    if(action==='capture') $('marker_unchanged').checked=false;
    message(action==='configure'?'Setup ready. Continue to step 2.':['validation/move','validation/return'].includes(action)?'Motion requested. Keep this tab active; Stop motion is available in Current operation.':'Working — keep the robot stationary.');
    await poll();
    if(action==='configure'&&!state.error) { activeStep=availableStep(); restoreCapture(); }
  } catch(e) { message(e.message,true); }
  finally { posting=false; render(); }
}
async function poll() {
  try {
    state=await request('/api/state');
    connectionOK=true;
    if(state.manual_handoff) message('Current operation owns hardware. Review its route and pending confirmation in Current operation. Reopen setup after the operation finishes.');
    $('teacher-job-status').textContent=state.manual_handoff?'Current operation owns hardware':teacherJob?'Connected · '+(state.motion?.error||state.motion?.message||state.error||state.motion?.pending||state.busy||'Idle')+' · job '+teacherJob+' · '+(state.job_state||'unknown'):'Connected · no selected job';
    const resultKey=JSON.stringify([state.completed,state.failed,state.error]);
    if(!state.busy && resultKey!==previousResult && (!dirty||state.requires_setup)) {
      if(state.error) message(state.error,true);
      else if(state.completed) message({configure:'Setup ready. Continue to step 2.',capture:'Marker captured. Keep it fixed and teach the pick endpoint in step 3.',teach:'Picking position saved. Use the pendant to position the robot before step 4.',locate:'Fresh poses exported. Review the files or open Manual PICK / PLACE.',capture_validation:'Validation capture ready. Review the target and clear path before motion.',validate_move:'Joint test completed. Independently check alignment and record the result.',return_observation:activeStep===2?'Returned to the saved observation joints. Continue to step 4 to read and export.':'Returned to the saved observation joints. Choose Capture for validation.'}[state.completed]);
      if(['capture','locate'].includes(state.completed)||['capture','locate'].includes(state.failed)) {
        loadCapture().catch(()=>{$('capture-image').hidden=true;});
      }
    }
    if(['validate_move','return_observation'].includes(state.busy)&&!document.hidden)await request('/api/validation/heartbeat',{});
    previousResult=resultKey; render();
  } catch(e) { connectionOK=false; $('observation-return-confirmed').checked=false; $('teacher-job-status').textContent='Connection lost'; render(); message('Teacher connection lost: '+e.message,true); $('capture').disabled=$('teach').disabled=$('locate').disabled=true; }
}
steps.forEach((step,index)=>$('nav-'+step).addEventListener('click',()=>showStep(index)));
$('back').addEventListener('click',()=>showStep(activeStep-1));
$('next').addEventListener('click',()=>showStep(activeStep+1));
$('setup').addEventListener('submit',async e=>{
  e.preventDefault();
  try {
    // Handle validation here so a field inside closed advanced settings cannot
    // silently prevent the submit event and leave the user without feedback.
    const invalid=$('setup').querySelector('input:invalid, select:invalid');
    if(invalid) {
      const details=invalid.closest('details'); if(details) details.open=true;
      const label=invalid.labels?.[0]?.firstChild?.textContent.trim()
        ||invalid.labels?.[0]?.textContent.trim()||invalid.id;
      activeStep=0; render();
      message('Check '+label+': '+invalid.validationMessage,true);
      invalid.focus(); invalid.reportValidity(); return;
    }
    await post('configure',settings());
  } catch(e) { message(e.message,true); }
});
$('open').addEventListener('click',async()=>{
  try {
    const saved=await request('/api/open',{project:$('project').value});
    fill(saved); await window.applyTestProfile(); dirty=true; activeStep=0; $('tcp_confirmed').checked=false;
    message('Saved project loaded. Check its active TCP on the pendant, then complete setup to continue.'); render();
  } catch(e) { message(e.message,true); }
});
$('setup').addEventListener('input',event=>{initialEdits?.add(event.target.id);dirty=true;activeStep=0;targetFields();$('observation-return-confirmed').checked=false;$('tcp_confirmed').checked=false;$('marker_unchanged').checked=false;message('Setup changed. Check the TCP and complete step 1 again.');render();});
// Checking the declaration itself must not immediately uncheck it.
$('tcp_confirmed').addEventListener('input',e=>{e.stopPropagation();render();});
$('calibration').addEventListener('change',()=>{const item=defaults.calibrations.find(c=>c.path===$('calibration').value);if(item){$('tcp').value=tcpText(item.tcp);$('robot_ip').value=item.robot_ip;}showCalibrationTCP();});
$('kind').addEventListener('change',targetFields);
$('picking-board').addEventListener('input',()=>{
  const value=$('picking-board').value;
  const board=value===''?null:defaults.picking_boards[Number(value)];
  if(board) { $('manifest').value=board.target.board_manifest; $('board').value=board.target.board; }
  targetFields();
});
$('manifest').addEventListener('change',targetFields);
$('board').addEventListener('change',targetFields);
$('marker_unchanged').addEventListener('change',render);
$('continue-pick-place').addEventListener('click',()=>{
  const p=state?.project,sim=simulatedProject?.settings;if(dirty||state?.busy||posting||(!p?.flow?.taught&&!sim))return;
  const settings=sim||p.settings,target=settings.target||{};
  localStorage.setItem('ano-handoff-pick-place',JSON.stringify({project:sim?settings.project:p.project,calibration:settings.calibration,manifest:target.board_manifest||target.aruco_manifest||'',simulation:!!sim}));
  location.href='/pick-place';
});
$('capture').addEventListener('click',()=>post('capture',{}));
$('teach').addEventListener('click',()=>post('teach',{marker_unchanged:$('marker_unchanged').checked}));
$('locate').addEventListener('click',()=>post('locate',{}));
$('validation-capture').addEventListener('click',()=>{ $('validation-confirmed').checked=false; post('validation/capture',{}); });
$('validation-confirmed').addEventListener('change',render);
$('observation-return-confirmed').addEventListener('change',render);
$('validation-collision-scene').addEventListener('input',()=>localStorage.setItem('ano-teacher-motion-form',JSON.stringify({'validation-collision-scene':$('validation-collision-scene').value})));
function workspace(id) {
  const text=$(id).value.trim(), values=text.split(/[\s,]+/).map(Number);
  if(!text||values.length!==3||!values.every(Number.isFinite)) throw new Error('Workspace needs three XYZ numbers in millimetres.');
  return values.map(v=>v/1000);
}
for(const id of ['validation-speed','validation-collision-scene','validation-workspace-min','validation-workspace-max','observation-return-speed']) $(id).addEventListener('input',()=>{$('validation-confirmed').checked=false;$('observation-return-confirmed').checked=false;render();});
async function returnToObservation(){
  try {
    if(!$('observation-return-confirmed').checked) throw new Error('Review and confirm the return joint path first.');
    const body={path_confirmed:$('observation-return-confirmed').checked,collision_scene:$('validation-collision-scene').value.trim(),
      joint_speed_deg_s:Number($('observation-return-speed').value),
      workspace_min_m:workspace('validation-workspace-min'),workspace_max_m:workspace('validation-workspace-max')};
    $('validation-confirmed').checked=false;
    $('observation-return-confirmed').checked=false;
    state.return_result=null;
    await post('validation/return',body);
  } catch(e) { message(e.message,true); }
}
for(const id of ['observation-return','teach-observation-return']) $(id).addEventListener('click',returnToObservation);
$('validation-move').addEventListener('click',async()=>{
  try {
    const body={capture_id:state.project.validation.id,path_confirmed:$('validation-confirmed').checked,
      joint_speed_deg_s:Number($('validation-speed').value),collision_scene:$('validation-collision-scene').value.trim(),workspace_min_m:workspace('validation-workspace-min'),workspace_max_m:workspace('validation-workspace-max')};
    $('validation-confirmed').checked=false;
    await post('validation/move',body);
  } catch(e) { message(e.message,true); }
});
async function stopValidation() {
  $('observation-return-confirmed').checked=false;
  $('validation-confirmed').checked=false;
  try { await request('/api/validation/stop',{}); await poll(); message('Stop requested. Capture again before another motion attempt.'); }
  catch(e) { message('Stop request could not reach the teacher. Use the pendant stop: '+e.message,true); }
}
$('validation-stop').addEventListener('click',stopValidation);
for(const [id,result] of [['validation-aligned','aligned'],['validation-misaligned','misaligned']]) $(id).addEventListener('click',async()=>{
  try { const text=$('validation-measured-error').value.trim();
    const measured_error_mm=text?text.split(/[\s,]+/).map(Number):null;
    await request('/api/validation/review',{capture_id:state.project.validation.id,result,measured_error_mm}); await poll(); message(result==='aligned'?'Alignment recorded. Reposition and capture again to test another pose.':'Alignment needs adjustment recorded. Review teaching and calibration before another test.'); }
  catch(e) { message(e.message,true); }
});
$('validation-image').addEventListener('error',()=>{$('validation-image').hidden=true;$('validation-placeholder').hidden=false;});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&['validate_move','return_observation'].includes(state?.busy)) stopValidation();});
$('capture-image').addEventListener('error',()=>{$('capture-image').hidden=true;render();});
for(const id of ['tcp_confirmed','marker_unchanged','validation-confirmed','observation-return-confirmed']) $(id).checked=false;
(async()=>{
  try {
    const savedMotion=JSON.parse(localStorage.getItem('ano-teacher-motion-form')||'{}');
    if(typeof savedMotion['validation-collision-scene']==='string') $('validation-collision-scene').value=savedMotion['validation-collision-scene'];
    defaults=await request('/api/defaults');
    for(const [index,board] of (defaults.picking_boards||[]).entries()) {
      const option=document.createElement('option'); option.value=String(index);
      option.textContent=board.target.board+' · '+board.dimensions_mm.join(' × ')+' mm · '+board.dictionary;
      $('picking-board').append(option);
    }
    for(const [id,items] of [['calibrations',defaults.calibrations.map(c=>c.path)],['manifests',defaults.manifests]])
      for(const item of items){const option=document.createElement('option');option.value=item;$(id).append(option);}
    await poll();
    const initial=state.project?{...state.project.settings,project:state.project.project}:defaults.settings;
    const prefill=(id,value,property)=>{if(!initialEdits.has(id))setField(id,value,property);};
    fill(initial,prefill);
    const handoff=JSON.parse(localStorage.getItem('ano-handoff-teaching')||'null');
    localStorage.removeItem('ano-handoff-teaching');
    if(handoff&&typeof handoff.calibration==='string'&&!initialEdits.has('calibration')){
      $('calibration').value=handoff.calibration;$('calibration').dataset.handoff='true';
      prefill('camera_provider',handoff.camera_provider||'realsense');
      if(handoff.robot_ip)prefill('robot_ip',handoff.robot_ip);
      const tcp=defaults.calibrations.find(c=>c.path===handoff.calibration)?.tcp??handoff.tcp;
      prefill('tcp',Array.isArray(tcp)&&tcp.length===6&&tcp.every(Number.isFinite)?tcpText(tcp):'');
      $('teaching-handoff').textContent=(handoff.simulation?'Simulated calibration. No physical qualification. ':'Selected calibration. ')+(handoff.serial?'Camera serial: '+handoff.serial+'. ':'')+'Select the picking board and confirm the TCP.';
      dirty=true;showCalibrationTCP();
    }
    initialEdits=null;
    await window.applyTestProfile();
    activeStep=state.manual_handoff?4:dirty?0:state.project?.flow?.taught?3:availableStep();
    $('tcp_confirmed').checked=false; $('marker_unchanged').checked=false;
    restoreCapture();
    render();
    setInterval(poll,700);

  } catch(e){message(e.message,true);}
})();

const retry=document.createElement('button');retry.id='retry-teacher';retry.className='secondary';retry.textContent='Retry the same request';retry.hidden=!teacherPending;
document.querySelector('#current-operation .operation-body').prepend(retry);
retry.addEventListener('click',async()=>{try{await sendTeacher();await poll();}catch(e){message(e.message,true);}});
document.querySelectorAll('a[href^="/api/download/"]').forEach(link=>link.addEventListener('click',async e=>{
  e.preventDefault();try{const name=link.getAttribute('href').split('/').pop();const blob=await loadTeacherFile(name);const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}catch(error){message(error.message,true);}
}));
