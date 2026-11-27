'use strict';
function multiply(a,b) {return a.map((row,i)=>row.map((_,j)=>row.reduce((s,__,k)=>s+a[i][k]*b[k][j],0)));}
function rigid(p) {
  const [r,y,w]=p.rpy_rad,[cr,sr,cp,sp,cy,sy]=[Math.cos(r),Math.sin(r),Math.cos(y),Math.sin(y),Math.cos(w),Math.sin(w)],v=p.xyz_m;
  return [[cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr,v[0]],[sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr,v[1]],[-sp,cp*sr,cp*cr,v[2]],[0,0,0,1]];
}
function point(t,v) {return t.slice(0,3).map(row=>row.slice(0,3).reduce((s,n,i)=>s+n*v[i],row[3]));}
function identity() {return [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]];}
function finiteVector(v,n=3,bound=10) {return Array.isArray(v)&&v.length===n&&v.every(x=>typeof x==='number'&&Number.isFinite(x)&&Math.abs(x)<=bound);}
function robotFrames(q,model) {
  if(!finiteVector(q,6,4*Math.PI)) throw Error('Enter six valid joint angles.');
  const out=[identity()];
  model.joints.forEach((joint,i)=>out.push(multiply(multiply(out.at(-1),rigid(joint)),rigid({xyz_m:[0,0,0],rpy_rad:[0,0,q[i]]}))));
  return out;
}
function boxPoints(b) {
  const t=rigid(b.pose),pts=[];
  for(const x of [-1,1])for(const y of [-1,1])for(const z of [-1,1])pts.push(point(t,[x*b.dimensions_m[0]/2,y*b.dimensions_m[1]/2,z*b.dimensions_m[2]/2]));
  return pts;
}
function exactKeys(o,keys) {return o&&typeof o==='object'&&!Array.isArray(o)&&Object.keys(o).sort().join('|')===keys.slice().sort().join('|');}
function validateScene(d,hash) {
  const keys=['version','type','frame','model_sha256','confirmed','environment_complete','clearance_m','model_uncertainty_m','attached','obstacles','notes'];
  if(!(exactKeys(d,keys)||exactKeys(d,[...keys,'collision_scope']))||d.version!==1||d.type!=='ur10e_collision_scene'||d.frame!=='base'||d.model_sha256!==hash||typeof d.confirmed!=='boolean'||typeof d.environment_complete!=='boolean'||typeof d.notes!=='string'||d.notes.length>2000||('collision_scope' in d&&!['cell','supervised_self'].includes(d.collision_scope))) throw Error('This file does not match the robot scene format or model.');
  if(!['clearance_m','model_uncertainty_m'].every(k=>typeof d[k]==='number'&&Number.isFinite(d[k])&&d[k]>=.0001&&d[k]<=.1)) throw Error('Set clearance and model uncertainty from 0.1 to 100 mm.');
  if(!Array.isArray(d.attached)||d.attached.length!==3||!Array.isArray(d.obstacles)||d.obstacles.length>40) throw Error('Include the tool, camera and mount. Use at most 40 obstacles.');
  if(d.collision_scope==='supervised_self'&&(d.obstacles.length||d.environment_complete)) throw Error('Supervised self scope cannot include cell coverage or cell boxes.');
  const names=new Set(),arm=['base_link_inertia','shoulder_link','upper_arm_link','forearm_link','wrist_1_link','wrist_2_link','wrist_3_link'];
  for(const b of [...d.attached,...d.obstacles]) {
    if(!exactKeys(b,['name','dimensions_m','pose'])||typeof b.name!=='string'||!/^[A-Za-z0-9_]{1,40}$/.test(b.name)||names.has(b.name)||!finiteVector(b.dimensions_m)||b.dimensions_m.some(x=>x<=0)||!exactKeys(b.pose,['xyz_m','rpy_rad'])||!finiteVector(b.pose.xyz_m)||!finiteVector(b.pose.rpy_rad,3,2*Math.PI)) throw Error('A box has an invalid name, dimension or pose.');
    if(d.obstacles.includes(b)&&[...arm,'tool','camera','mount'].includes(b.name)) throw Error('An obstacle name conflicts with a robot part.');
    names.add(b.name);
  }
  if(d.attached.map(b=>b.name).sort().join('|')!=='camera|mount|tool') throw Error('Name the attached boxes tool, camera and mount.');
  return JSON.parse(JSON.stringify(d));
}
function readJointPlan(d,hash) {
  let segments;
  if(d.type==='ur10e_checked_joint_plan') {
    if(d.version!==1||d.model_sha256!==hash) throw Error('This path file uses a different robot model.');
    validateScene(d.scene,hash);segments=d.segments;
  } else {
    if(!finiteVector(d.start_joints,6,4*Math.PI)||!Array.isArray(d.plan?.targets)) throw Error('This file has no calibration joint plan.');
    segments=[];let q=d.start_joints;
    for(const t of d.plan.targets) {
      if(t.via_start){segments.push({label:'via_start',start_joints:q,target_joints:d.start_joints});q=d.start_joints;}
      segments.push({label:t.label,start_joints:q,target_joints:t.joints});q=t.joints;
    }
    segments.push({label:'return_start',start_joints:q,target_joints:d.start_joints});
  }
  if(!Array.isArray(segments)||segments.length<1||segments.length>200||segments.some(s=>typeof s.label!=='string'||s.label.length>80||!finiteVector(s.start_joints,6,4*Math.PI)||!finiteVector(s.target_joints,6,4*Math.PI)||s.status&&!['clear_in_model','collision','unresolved','unavailable'].includes(s.status))) throw Error('The joint path file is not valid.');
  return {segments,scene:d.scene||null};
}
function canonical(v) {if(Array.isArray(v))return '['+v.map(canonical).join(',')+']';if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}';return JSON.stringify(v);}
if(typeof module!=='undefined')module.exports={multiply,rigid,point,robotFrames,boxPoints,validateScene,readJointPlan,canonical};

if(typeof document!=='undefined')document.addEventListener('DOMContentLoaded',()=>{
  const native=document.getElementById('native-tool-preview'),visualOnly=!!native||new URLSearchParams(location.search).get('view')==='preview';
  const calibrationFrame=new URLSearchParams(location.search).get('view')==='calibration'&&window.parent!==window;
  const section=document.createElement('section');section.className='panel robot-panel'+(visualOnly?' visual-only':'');section.id='robot-panel';
  section.innerHTML=`<h2>UR10e and tool preview</h2><p class="badge">NOMINAL MODEL · PREVIEW</p>
  <p>Drag to orbit. Right-drag to pan. Use the wheel to zoom.</p><p class="hint">Focus the view. Use arrow keys to orbit. Use + or - to zoom.</p><button id="fit-view" type="button">Fit view</button><p id="preview-status" role="status"></p>
  <div class="robot-layout"><div><canvas id="robot-canvas" width="1000" height="620" tabindex="0" aria-label="${visualOnly?'UR10e, tool and schematic camera preview':'UR10e, tool, camera, mount and cell preview'}"></canvas>
  
  <p class="legend">${visualOnly?'Grey: UR10e. Orange: tool. Blue: schematic camera.':'Grey: UR10e. Orange: tool. Blue: camera. Purple: mount. Green: cell.'}</p><p class="hint">Axes at base and tool0: X red, Y green, Z blue.</p>
  <p>Box dimensions and positions use millimetres. Box angles and joint angles use degrees.</p>
  <p>${visualOnly?(native?'Tool geometry is saved in the collision scene. The camera graphic is visual only.':'Visual reference only. These boxes do not establish motion clearance.'): "The preview shows the robot visual model and collision boxes. The nominal model uses generic UR10e data. It does not include this robot's factory calibration."}</p>
  <p id="path-status" role="status">Example joint pose. No path check is loaded.</p>
  <div class="full-scene"><label>Path file <input type="file" id="path-import" accept=".json,application/json"></label>
  <label>Path segment <select id="path-segment" disabled></select></label>
  <label>Position along segment <input id="path-position" type="range" min="0" max="1000" value="0" disabled></label>
  <div id="joint-inputs" class="grid"></div></div><label>Robot IP <input id="pose-ip" autocomplete="off"></label><button id="pose-read" type="button" disabled>Use robot pose</button><p id="pose-status" role="status">Checking pose access.</p></div>
  <div><h3>Tool box</h3><div class="full-scene"><p>Mount is the camera support bracket. Mounting offsets are unverified starting values.</p><label>Box <select id="scene-box"></select></label><p id="scene-frame" class="hint" aria-live="polite"></p></div><p id="camera-visual-status" role="status" ${visualOnly?'':'hidden'}>Camera visual appears after calibration.</p><div id="tool-slot"></div><div class="full-scene"><div id="scene-fields" class="grid"></div>
  <button id="add-obstacle" type="button">Add cell box</button><button id="remove-obstacle" class="secondary" type="button">Remove cell box</button>
  <details><summary>Scene settings</summary><label>Clearance (mm) <input id="scene-clearance" type="number" min="0.1" max="100" step="0.1" value="5"></label>
  <label>Model uncertainty (mm) <input id="scene-uncertainty" type="number" min="0.1" max="100" step="0.1" value="3"></label>
  <p class="hint">Model uncertainty is the maximum position error for each body. Measure this error before motion.</p>
  </details></div><div class="scene-approval" ${visualOnly&&!native?'hidden':''}><div class="scene-checks"><p id="scene-scope" role="status"></p><label class="check"><input type="checkbox" id="scene-confirmed"> <span id="scene-confirmed-label">Physical model checked</span></label>
  <p class="hint">Check all shapes and offsets against the physical robot. Check robot kinematics, cable coverage and model uncertainty.</p>
  <div id="cell-coverage"><label class="check"><input type="checkbox" id="scene-complete"> Cell coverage checked</label>
  <p class="hint">Include the table, fixed board, dummy and nearby objects. Add at least one measured cell box.</p></div>
  </div><p id="scene-loaded" role="status">Example scene.</p><button id="scene-reload" type="button" ${native?'':'hidden'}>Load selected scene</button><button id="scene-save" type="button" disabled>Save scene for Plan</button><p id="scene-message" role="alert"></p><p id="scene-state" class="badge">AUTOMATIC MOTION BLOCKED</p></div><div class="full-scene"><button id="scene-export" type="button">Export collision scene</button><label class="file secondary">Import collision scene<input type="file" id="scene-import" accept=".json,application/json"></label>
  <p>This page cannot move the robot. The calibration application checks the scene and path before each joint move.</p>
  <p>The teacher uses this page for viewing only. Teacher motion does not use these checks.</p></div></div></div>
  <p class="hint">© 2023 Universal Robots A/S. <a href="UR-MODEL-NOTICE.txt">Model terms and notices</a>.</p>`;
  (native||document.querySelector('main')).insertBefore(section,document.getElementById('tool-diagrams'));
  if(visualOnly)document.getElementById('tool-diagrams').hidden=true;
  document.getElementById('tool-slot').append(document.getElementById('tool-editor'));
  if(native){elNative('tool-editor').querySelector('.badge').textContent='TOOL COLLISION ENVELOPE';elNative('tool-editor').querySelector('.hint').textContent='Measure the tool box. Save the scene for Plan.';}
  function elNative(id){return document.getElementById(id);}
  const el=id=>document.getElementById(id),copy=v=>JSON.parse(JSON.stringify(v));
  let scene=copy(DEFAULT_COLLISION_SCENE),q=[0,-1.9,-1.6,-1.2,1.5708,0],plan=null,bodyValid=true,poseRevision=0,sceneRevision=0,readRevision=0,loadingScene=false,cameraVisual=null,nativeLoaded=false,nativeSelection='',nativeBase='',nativeTimer,nativeDirty=false;
  const state=el('scene-state'),message=el('scene-message'),canvas=el('robot-canvas');
  const render=typeof createRobotPreview==='function'?createRobotPreview(canvas,el('preview-status'),el('fit-view')):()=>{};
  function draw(){render(q,scene,UR10E_MODEL,visualOnly||calibrationFrame?cameraVisual:undefined);}
  const jointInputs=[];
  for(let i=0;i<6;i++){const label=document.createElement('label');label.textContent=['Base','Shoulder','Elbow','Wrist 1','Wrist 2','Wrist 3'][i]+' (deg)';const input=document.createElement('input');input.type='number';input.min=-720;input.max=720;input.step=.1;input.value=(q[i]*180/Math.PI).toFixed(2);input.addEventListener('input',()=>{poseRevision++;el('pose-status').textContent='Manual joint pose.';const v=jointInputs.map(n=>n.value.trim()===''?NaN:Number(n.value)*Math.PI/180);if(finiteVector(v,6,4*Math.PI)){q=v;plan=null;el('path-status').textContent='Manual joint pose. No path check applies.';draw();}});label.append(input);jointInputs.push(input);el('joint-inputs').append(label);}
  const fieldKeys=['Length','Width','Height','Centre X','Centre Y','Centre Z','Roll','Pitch','Yaw'],bodyInputs=[];let bodyDisplayed=[];
  for(let i=0;i<9;i++){const label=document.createElement('label');label.textContent=fieldKeys[i]+(i<6?' (mm)':' (deg)');const input=document.createElement('input');input.type='number';input.step='any';input.id='body-'+i;input.addEventListener('input',editBody);label.append(input);bodyInputs.push(input);el('scene-fields').append(label);}
  function selected(){return [...scene.attached,...scene.obstacles].find(b=>b.name===el('scene-box').value);}
  function options(){const previous=el('scene-box').value;el('scene-box').replaceChildren();for(const b of [...scene.attached,...scene.obstacles]){const o=document.createElement('option');o.value=b.name;o.textContent=b.name;el('scene-box').append(o);}if([...el('scene-box').options].some(o=>o.value===previous))el('scene-box').value=previous;showBody();}
  function showBody(){const b=selected();if(!b)return;const tool=b.name==='tool';el('tool-editor').hidden=!tool;el('scene-fields').hidden=tool;el('scene-frame').textContent=scene.obstacles.includes(b)?'Cell box pose uses base coordinates.':'Attached box pose uses tool0 (flange) coordinates.';bodyValid=true;const v=[...b.dimensions_m.map(x=>x*1000),...b.pose.xyz_m.map(x=>x*1000),...b.pose.rpy_rad.map(x=>x*180/Math.PI)];bodyDisplayed=v.map(x=>Number(x.toFixed(6)));bodyInputs.forEach((n,i)=>n.value=bodyDisplayed[i]);el('remove-obstacle').disabled=!scene.obstacles.includes(b);}
  function draftKey(path){return 'ano-scene-draft-'+UR_MODEL_SHA256+':'+path;}
  const toolIds=['length','width','height','x','y','z','roll','pitch','yaw'];
  function storeDraft(){if(nativeLoaded&&nativeSelection&&nativeDirty)try{localStorage.setItem(draftKey(nativeSelection),JSON.stringify({base:nativeBase,scene,fields:toolIds.map(id=>el(id).value)}));}catch{message.textContent='Draft could not be stored locally. Save the scene before leaving.';}}
  function sceneDirty(){if(loadingScene)return;sceneRevision++;if(native||calibrationFrame){nativeDirty=true;setTimeout(storeDraft,0);}if(!native&&location.protocol!=='file:'&&window.parent!==window)window.parent.postMessage({type:'calibration-scene-dirty'},location.origin);}
  function changed(){sceneDirty();scene.confirmed=false;scene.environment_complete=false;message.textContent=scene.collision_scope==='supervised_self'?'The scene changed. Check the arm and attached model again.':'The scene changed. Check the physical model and cell again.';sync();draw();}
  function editBody(){const v=bodyInputs.map(n=>n.value.trim()===''?NaN:Number(n.value)),next=copy(scene),b=[...next.attached,...next.obstacles].find(x=>x.name===selected()?.name);if(!b)return;v.forEach((x,i)=>{if(x!==bodyDisplayed[i]){if(i<3)b.dimensions_m[i]=x/1000;else if(i<6)b.pose.xyz_m[i-3]=x/1000;else b.pose.rpy_rad[i-6]=x*Math.PI/180;}});try{validateScene(next,UR_MODEL_SHA256);bodyDisplayed=v;bodyValid=true;scene=next;changed();}catch{bodyValid=false;message.textContent='Enter valid dimensions and offsets. The view keeps the last valid scene.';}}
  function sync(){const self=scene.collision_scope==='supervised_self';el('scene-confirmed').checked=scene.confirmed;el('scene-complete').checked=scene.environment_complete;el('cell-coverage').hidden=self;el('scene-complete').disabled=self||(!!native&&!nativeLoaded);el('scene-confirmed-label').textContent=self?'Arm and attached model checked':'Physical model checked';el('scene-scope').textContent=native&&!nativeLoaded?'Scene scope is not loaded.':self?'Arm and attached tools only. External objects are not checked.':'Cell scope. Check external objects and cell coverage.';el('scene-clearance').value=scene.clearance_m*1000;el('scene-uncertainty').value=scene.model_uncertainty_m*1000;state.textContent=scene.confirmed&&(self||(scene.environment_complete&&scene.obstacles.length))?'SCENE RECORDED · PATH CHECK REQUIRED':'AUTOMATIC MOTION BLOCKED';showPathStatus();}
  function showPathStatus(){if(!plan)return;const segment=plan.segments[Number(el('path-segment').value)||0];const same=plan.scene&&canonical(plan.scene)===canonical(scene);el('path-status').textContent=same&&segment.status?'Recorded path check: '+segment.status.replaceAll('_',' ')+'. This is not a live check.':'This scene has no matching path check. Run the offline path check.';}
  el('scene-box').addEventListener('change',showBody);
  el('add-obstacle').addEventListener('click',()=>{if(scene.collision_scope==='supervised_self'){message.textContent='Cell boxes are not allowed in supervised self scope.';return;}if(scene.obstacles.length>=40){message.textContent='Use at most 40 cell boxes.';return;}let i=1;while(scene.obstacles.some(b=>b.name==='obstacle_'+i))i++;scene.obstacles.push({name:'obstacle_'+i,dimensions_m:[.2,.2,.2],pose:{xyz_m:[.6,0,.1],rpy_rad:[0,0,0]}});options();el('scene-box').value='obstacle_'+i;showBody();changed();});
  el('remove-obstacle').addEventListener('click',()=>{scene.obstacles=scene.obstacles.filter(b=>b!==selected());options();changed();});
  for(const [id,key] of [['scene-clearance','clearance_m'],['scene-uncertainty','model_uncertainty_m']])el(id).addEventListener('change',()=>{const old=scene[key];scene[key]=Number(el(id).value)/1000;try{validateScene(scene,UR_MODEL_SHA256);changed();}catch{scene[key]=old;message.textContent='Set clearance and model uncertainty from 0.1 to 100 mm.';}});
  for(const [id,key] of [['scene-confirmed','confirmed'],['scene-complete','environment_complete']])el(id).addEventListener('change',()=>{if(key==='environment_complete'&&scene.collision_scope==='supervised_self'){sync();return;}scene[key]=el(id).checked;sceneDirty();sync();});
  function download(data,name){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function checkedScene(){if(!bodyValid||[...bodyInputs,...['length','width','height','x','y','z','roll','pitch','yaw','scene-clearance','scene-uncertainty'].map(el)].some(n=>String(n.value).trim()===''||!Number.isFinite(Number(n.value))||!n.checkValidity())||(el('tool-message')||el('message')).textContent)throw Error('Check the scene values.');for(const [id,key] of [['scene-clearance','clearance_m'],['scene-uncertainty','model_uncertainty_m']])if(Number(el(id).value)/1000!==scene[key])throw Error('Check the scene values.');return validateScene(scene,UR_MODEL_SHA256);}
  el('scene-export').addEventListener('click',()=>{try{download(checkedScene(),'collision-scene.json');message.textContent='';}catch{message.textContent='The editor cannot export this scene. Check the values.';}});
  async function fileData(event){const file=event.target.files[0];if(!file)return null;if(file.size>1000000)throw Error('Use a JSON file smaller than 1 MB.');try{return JSON.parse(await file.text());}catch{throw Error('The file is not valid JSON.');}}
  function importScene(data){const next=validateScene(data,UR_MODEL_SHA256),tool=next.attached.find(b=>b.name==='tool');const vals=[...tool.dimensions_m.map(x=>x*1000),...tool.pose.xyz_m.map(x=>x*1000),...tool.pose.rpy_rad.map(x=>x*180/Math.PI)];if(vals.slice(0,3).some(x=>x>5000)||vals.slice(3,6).some(x=>Math.abs(x)>5000))throw Error('The tool exceeds the tool box editor limits.');['length','width','height','x','y','z','roll','pitch','yaw'].forEach((id,i)=>el(id).value=vals[i]);el('coverage').value='tool-only';loadingScene=true;try{el('length').dispatchEvent(new Event('input'));scene=next;options();sync();draw();message.textContent='';}finally{loadingScene=false;}}
  function restoreToolDraft(path,result){
    const base=canonical(validateScene(result.scene,UR_MODEL_SHA256));
    let draft;
    try{
      const value=JSON.parse(localStorage.getItem(draftKey(path))||'null');
      if(value&&Array.isArray(value.fields)&&value.fields.length===toolIds.length){
        validateScene(value.scene,UR_MODEL_SHA256);
        draft=value;
      }
    }catch{}
    const next=copy(calibrationFrame&&draft?.base===base?draft.scene:result.scene);
    if(draft){
      next.attached[next.attached.findIndex(b=>b.name==='tool')]=copy(draft.scene.attached.find(b=>b.name==='tool'));
      next.confirmed=draft.base===base&&draft.scene.confirmed;
      next.environment_complete=draft.base===base&&draft.scene.environment_complete;
    }
    importScene(next);
    if(draft){
      loadingScene=true;
      try{toolIds.forEach((id,i)=>el(id).value=draft.fields[i]);el('length').dispatchEvent(new Event('input'));}
      finally{loadingScene=false;}
      message.textContent=draft.base===base?'Unsaved draft restored.':'The saved scene changed. The tool draft was restored. Check the scene again.';
    }
    nativeSelection=path;nativeBase=base;nativeLoaded=true;nativeDirty=Boolean(draft);sync();
  }
  el('scene-import').addEventListener('change',async event=>{try{const data=await fileData(event);if(!data)return;readRevision++;importScene(data);el('scene-loaded').textContent='Imported file: '+event.target.files[0].name;el('scene-save').disabled=location.protocol==='file:';sceneDirty();}catch(e){message.textContent='The editor did not import the scene. '+e.message;}finally{event.target.value='';}});
  document.addEventListener('tool-box-change',event=>{const p=event.detail;scene.attached.find(b=>b.name==='tool').dimensions_m=p.dimensions_m;scene.attached.find(b=>b.name==='tool').pose=copy(p.pose_in_flange);if(native){if(loadingScene)draw();else changed();}else if(visualOnly){if(!loadingScene)try{localStorage.setItem('ano-tool-preview-'+UR_MODEL_SHA256,JSON.stringify({profile:p,values:['length','width','height','x','y','z','roll','pitch','yaw','margin'].map(id=>el(id).value)}));}catch{}draw();}else changed();});
  el('path-import').addEventListener('change',async event=>{try{const d=await fileData(event);if(!d)return;plan=readJointPlan(d,UR_MODEL_SHA256);el('path-segment').replaceChildren();plan.segments.forEach((s,i)=>{const o=document.createElement('option');o.value=i;o.textContent=(i+1)+': '+s.label;el('path-segment').append(o);});el('path-segment').disabled=el('path-position').disabled=false;el('path-position').value=0;pathPose();message.textContent='';}catch(e){message.textContent='The editor did not import the path. '+e.message;}finally{event.target.value='';}});
  function pathPose(){if(!plan)return;poseRevision++;el('pose-status').textContent='Path pose.';const s=plan.segments[Number(el('path-segment').value)||0],t=Number(el('path-position').value)/1000;q=s.start_joints.map((v,i)=>v*(1-t)+s.target_joints[i]*t);jointInputs.forEach((n,i)=>n.value=(q[i]*180/Math.PI).toFixed(2));showPathStatus();draw();}
  el('path-position').addEventListener('input',pathPose);el('path-segment').addEventListener('change',()=>{el('path-position').value=0;pathPose();});

  section.addEventListener('input',event=>{if(!loadingScene&&(event.target.closest('#tool-editor,#scene-fields')||['scene-clearance','scene-uncertainty','scene-confirmed','scene-complete'].includes(event.target.id))){sceneRevision++;if(native&&event.target.closest('#tool-fields')){scene.confirmed=false;scene.environment_complete=false;sync();sceneDirty();message.textContent=scene.collision_scope==='supervised_self'?'The tool changed. Check the arm and attached model again.':'The tool changed. Check the physical model and cell again.';}}},true);
  const token=document.querySelector('meta[name="module-token"]').content,poseButton=el('pose-read');
  try{const saved=JSON.parse(localStorage.getItem('ano-calibration-form')||'{}');if(typeof saved.robot_ip==='string')el('pose-ip').value=saved.robot_ip;}catch{}
  async function api(path,body){const opts={headers:{'X-Module-Token':token}};if(body){opts.method='POST';opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}const response=await fetch(path,opts),value=await response.json();if(!response.ok)throw Error(value.error||'Request failed.');return value;}
  if(location.protocol==='file:'||token==='__TOKEN__')el('pose-status').textContent='Robot pose is unavailable in direct file mode.';
  else {el('scene-save').disabled=!!native||window.parent!==window;api('/api/status').then(status=>{poseButton.disabled=status.allow_manual!==true;el('pose-status').textContent=poseButton.disabled?'Read-only pose access is disabled.':'Read a single robot pose snapshot.';}).catch(()=>{el('pose-status').textContent='Robot pose access is unavailable.';});}
  if(!visualOnly&&location.protocol!=='file:'&&window.parent!==window){
    window.addEventListener('message',async event=>{
      if(event.origin!==location.origin||event.source!==window.parent||event.data?.type!=='calibration-scene-select'||typeof event.data.path!=='string')return;
      if(calibrationFrame){storeDraft();nativeLoaded=false;}
      const selection=event.data.path,revision=++readRevision,edit=sceneRevision;el('scene-save').disabled=true;
      if(!selection){el('scene-loaded').textContent+=' No saved scene selected.';el('scene-save').disabled=false;return;}
      el('scene-loaded').textContent='Loading scene: '+selection;
      try{
        let job=await api('/api/calibration',{request_id:crypto.randomUUID(),action:'scene-read',path:selection});
        for(let i=0;job.state==='running'&&i<60;i++){await new Promise(resolve=>setTimeout(resolve,500));job=await api('/api/jobs/'+encodeURIComponent(job.id));}
        if(job.state!=='done')throw Error(job.error||'Scene read did not finish.');
        if(revision!==readRevision)return;
        if(edit!==sceneRevision)throw Error('The editor changed during loading. Select the scene again.');
        const result=job.result;
        if(!result||typeof result.path!=='string'||!result.path.startsWith('/')||result.model_sha256!==UR_MODEL_SHA256||result.confirmed!==result.scene?.confirmed||result.environment_complete!==result.scene?.environment_complete||result.obstacles_count!==result.scene?.obstacles?.length)throw Error('The scene read result is invalid.');
        if(calibrationFrame)restoreToolDraft(selection,result);else importScene(result.scene);
        el('scene-loaded').textContent='Loaded scene: '+result.path+(nativeDirty?' · unsaved tool draft':'');el('scene-save').disabled=false;
        window.parent.postMessage({type:'calibration-scene-loaded',selection,path:result.path,scope:scene.collision_scope||'cell',dirty:nativeDirty},location.origin);
      }catch(error){if(revision!==readRevision)return;el('scene-loaded').textContent='Scene not loaded: '+selection+'. '+error.message;window.parent.postMessage({type:'calibration-scene-load-error',selection,error:error.message},location.origin);}
    });
    window.parent.postMessage({type:'calibration-scene-ready'},location.origin);
  }
  if(native){
    function enabled(value){for(const id of [...toolIds,'scene-confirmed'])el(id).disabled=!value;el('scene-complete').disabled=!value||scene.collision_scope==='supervised_self';el('scene-save').disabled=!value;}
    function loadScene(path){
      if(nativeDirty)storeDraft();
      const revision=++readRevision;clearTimeout(nativeTimer);nativeLoaded=false;enabled(false);el('scene-scope').textContent='Scene scope is not loaded.';
      if(!path){el('scene-loaded').textContent='Select a collision scene in Plan. The view shows an example tool.';return;}
      el('scene-loaded').textContent='Loading scene: '+path+'. The view shows the previous tool.';
      nativeTimer=setTimeout(async()=>{
        try{
          let job=await api('/api/calibration',{request_id:crypto.randomUUID(),action:'scene-read',path});
          for(let i=0;job.state==='running'&&i<60;i++){await new Promise(resolve=>setTimeout(resolve,500));job=await api('/api/jobs/'+encodeURIComponent(job.id));}
          if(revision!==readRevision)return;
          if(job.state!=='done')throw Error(job.error||'Scene read did not finish.');
          const result=job.result;
          if(!result||typeof result.path!=='string'||!result.path.startsWith('/')||result.model_sha256!==UR_MODEL_SHA256||result.confirmed!==result.scene?.confirmed||result.environment_complete!==result.scene?.environment_complete||result.obstacles_count!==result.scene?.obstacles?.length)throw Error('The scene read result is invalid.');
          restoreToolDraft(path,result);
          enabled(true);sync();
          el('scene-loaded').textContent='Loaded scene: '+result.path+' · '+result.obstacles_count+' cell boxes'+(nativeDirty?' · unsaved draft':'');
          document.dispatchEvent(new CustomEvent('calibration-scene-scope',{detail:{selection:path,scope:scene.collision_scope||'cell'}}));
        }catch(error){if(revision===readRevision)el('scene-loaded').textContent='Scene not loaded: '+path+'. '+error.message;}
      },150);
    }
    document.addEventListener('calibration-scene-select',event=>loadScene(event.detail));
    el('scene-reload').addEventListener('click',()=>loadScene(el('collision_scene').value.trim()));
    loadScene(el('collision_scene').value.trim());
  }
  if(visualOnly||calibrationFrame){
    let revision=0,timer,queue=Promise.resolve();
    function selectCalibration(path){
      const current=++revision;clearTimeout(timer);cameraVisual=null;draw();
      const status=el('camera-visual-status');
      if(!path){status.textContent='Camera visual appears after calibration.';return;}
      if(location.protocol==='file:'||token==='__TOKEN__'){status.textContent='Camera visual requires the calibration service.';return;}
      status.textContent='Loading camera visual.';
      timer=setTimeout(()=>{queue=queue.catch(()=>{}).then(async()=>{
        if(current!==revision)return;
        try{
          let job=await api('/api/calibration',{request_id:crypto.randomUUID(),action:'preview-calibration',path});
          for(let i=0;job.state==='running'&&i<60;i++){await new Promise(resolve=>setTimeout(resolve,500));job=await api('/api/jobs/'+encodeURIComponent(job.id));}
          if(current!==revision)return;
          if(job.state!=='done')throw Error(job.error||'Calibration preview did not finish.');
          const r=job.result,m=r?.camera_in_flange;
          if(typeof r?.path!=='string'||!['good','acceptable','poor'].includes(r.verdict)||typeof r.simulated!=='boolean'||!Array.isArray(m)||m.length!==4||m.some(row=>!finiteVector(row,4,10000))||m[3].some((v,i)=>v!==[0,0,0,1][i]))throw Error('The camera pose is invalid.');
          cameraVisual=m;status.textContent='Schematic camera at optical origin · visual only · '+r.path+' · '+r.verdict.toUpperCase()+(r.simulated?' · SIMULATION':'')+(r.camera_serial?' · camera '+r.camera_serial:'');draw();
        }catch(error){if(current===revision){status.textContent='Camera visual unavailable. '+error.message;cameraVisual=null;draw();}}
      });},300);
    }
    if(calibrationFrame){
      el('camera-visual-status').hidden=false;
      window.addEventListener('keydown',event=>{
        if(event.key==='Escape'){
          event.preventDefault();
          if(!event.repeat)window.parent.postMessage({type:'calibration-escape'},location.origin);
        }
      },true);
      window.addEventListener('message',event=>{
        if(event.origin===location.origin&&event.source===window.parent&&event.data?.type==='calibration-preview-select'&&typeof event.data.path==='string')selectCalibration(event.data.path);
        if(event.origin===location.origin&&event.source===window.parent&&event.data?.type==='calibration-robot-select'&&typeof event.data.robot==='string')el('pose-ip').value=event.data.robot;
      });
    }
    else if(native){document.addEventListener('preview-calibration-selection',event=>selectCalibration(event.detail));selectCalibration(document.getElementById('calibration').value.trim());}
    else {try{const saved=JSON.parse(localStorage.getItem('ano-calibration-form')||'{}');selectCalibration(typeof saved.calibration==='string'?saved.calibration:'');}catch{selectCalibration('');}}
  }
  el('scene-save').addEventListener('click',async()=>{
    if(visualOnly&&!native)return;
    if(native&&!nativeLoaded)return;
    const button=el('scene-save'),selectionRevision=readRevision;button.disabled=true;
    try{
      const saved=checkedScene();message.textContent='Saving scene.';
      let job=await api('/api/calibration',{request_id:crypto.randomUUID(),action:'scene',scene:saved});
      for(let i=0;job.state==='running'&&i<60;i++){await new Promise(resolve=>setTimeout(resolve,500));job=await api('/api/jobs/'+encodeURIComponent(job.id));}
      if(job.state!=='done')throw Error(job.error||'Scene job did not finish. Check the service job status.');
      const result=job.result;
      if(!result||typeof result.path!=='string'||!result.path.startsWith('/')||result.model_sha256!==saved.model_sha256||result.confirmed!==saved.confirmed||result.environment_complete!==saved.environment_complete||result.obstacles_count!==saved.obstacles.length)throw Error('The scene save result is invalid.');
      if(selectionRevision!==readRevision||canonical(checkedScene())!==canonical(saved))throw Error('The scene changed during the save. Save it again.');
      message.textContent='Scene saved: '+result.path;el('scene-loaded').textContent='Saved scene: '+result.path+' · '+result.obstacles_count+' cell boxes';
      if(native||calibrationFrame){try{localStorage.removeItem(draftKey(nativeSelection));}catch{}nativeSelection=result.path;nativeBase=canonical(saved);nativeDirty=false;}
      if(native){document.dispatchEvent(new CustomEvent('calibration-scene-saved',{detail:result.path}));document.dispatchEvent(new CustomEvent('calibration-scene-scope',{detail:{selection:result.path,scope:scene.collision_scope||'cell'}}));}
      else if(window.parent!==window)window.parent.postMessage({type:'calibration-scene-saved',path:result.path,scope:saved.collision_scope||'cell'},location.origin);
    }catch(error){message.textContent='The scene was not applied. '+error.message;}
    finally{if(selectionRevision===readRevision)button.disabled=false;}
  });
  poseButton.addEventListener('click',async()=>{
    poseButton.disabled=true;const revision=poseRevision;el('pose-status').textContent='Reading robot pose.';
    try{
      let job=await api('/api/calibration',{request_id:crypto.randomUUID(),action:'pose',robot_ip:el('pose-ip').value.trim()});
      for(let i=0;job.state==='running'&&i<60;i++){await new Promise(resolve=>setTimeout(resolve,500));job=await api('/api/jobs/'+encodeURIComponent(job.id));}
      if(job.state!=='done')throw Error(job.error||'Pose job did not finish. Check the service job status.');
      const result=job.result;
      if(!result||!finiteVector(result.joints_rad,6,4*Math.PI)||result.source!=='primary_read_only'||typeof result.robot_ip!=='string'||typeof result.robot_timestamp_s!=='number'||!Number.isFinite(result.robot_timestamp_s)||typeof result.received_at_utc!=='string'||!Number.isFinite(Date.parse(result.received_at_utc)))throw Error('The pose result is invalid.');
      if(revision!==poseRevision)throw Error('The pose changed while the request was running.');
      q=result.joints_rad.slice();plan=null;jointInputs.forEach((n,i)=>n.value=(q[i]*180/Math.PI).toFixed(2));el('path-status').textContent='Robot pose snapshot. No path check applies.';el('pose-status').textContent='Robot pose snapshot: '+result.received_at_utc+' · '+result.robot_ip;draw();
    }catch(error){el('pose-status').textContent='Robot pose unavailable. '+error.message;}
    finally{poseButton.disabled=false;}
  });
  options();sync();draw();
  const tool=scene.attached.find(b=>b.name==='tool');['length','width','height','x','y','z','roll','pitch','yaw'].forEach((id,i)=>{const values=[...tool.dimensions_m.map(x=>x*1000),...tool.pose.xyz_m.map(x=>x*1000),...tool.pose.rpy_rad.map(x=>x*180/Math.PI)];el(id).value=values[i];});el('coverage').value='tool-only';
  if(visualOnly&&!native)try{const cached=JSON.parse(localStorage.getItem('ano-tool-preview-'+UR_MODEL_SHA256)),saved=window.parseToolProfile(cached.profile||cached),values=Array.isArray(cached.values)&&cached.values.length===10&&cached.values.every((v,i)=>Number.isFinite(Number(v))&&Math.abs(Number(v)-saved.values[i])<1e-9)?cached.values:saved.values;['length','width','height','x','y','z','roll','pitch','yaw','margin'].forEach((id,i)=>el(id).value=values[i]);el('coverage').value=saved.coverage;}catch{}
  loadingScene=true;try{el('length').dispatchEvent(new Event('input'));}finally{loadingScene=false;}
});
