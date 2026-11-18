/* Prefill only values discovered under the IPC data directory or an optional site profile. */
(() => {
  const el=id=>document.getElementById(id), edited=new Set(), applied=new Set();
  document.addEventListener('input',event=>{if(event.target.id)edited.add(event.target.id);});
  const vector=(value,n)=>!!value.trim()&&value.trim().split(/[\s,]+/).length===n&&value.trim().split(/[\s,]+/).every(v=>Number.isFinite(Number(v)));
  function put(id,value,valid=text=>!!text.trim()) {
    const field=el(id);if(!field||value===undefined||value===null)return;
    const untouchedDefault=!edited.has(id)&&!applied.has(id)&&field.value===field.defaultValue;
    if(!valid(field.value)||untouchedDefault){field.value=String(value);applied.add(id);}
  }
  async function get(path) {
    const token=document.querySelector('meta[name="module-token"],meta[name="teach-token"]').content;
    const response=await fetch(path,{headers:{'X-Module-Token':token}});
    if(!response.ok)throw new Error('Discovered setup is unavailable; review the saved project or enter checked settings.');
    return response.json();
  }
  window.applyTestProfile=async()=>{
    const note=el('test-profile-note');
    try {
      if(el('camera_provider')?.value==='basler'||el('calibration')?.dataset.handoff==='true'){
        note.textContent='Selected camera calibration retained. Enter and review the remaining setup.';return;
      }
      const found=await get('/api/teacher-defaults'), saved=found.settings||{}, target=saved.target||{};
      put('project',saved.project);
      put('calibration',saved.calibration);
      put('manifest',target.board_manifest);
      put('board',target.board);
      put('robot_ip',saved.robot_ip);
      put('camera_provider',saved.camera_provider);
      put('motion_model',saved.motion_model);
      put('approach_mm',saved.approach_mm);
      put('tcp',saved.tcp?.map((v,i)=>i<3?v*1000:v).join(', '),text=>vector(text,6));
      put('manual-pick-tag',target.board);
      put('manual-place-tag',target.board);
      if(el('manual-scene')||el('validation-collision-scene')){
        const control=await get('/api/robot-control'), d=control.defaults||{};
        put('manual-scene',d.collision_scene);put('validation-collision-scene',d.collision_scene);
        put('robot-ip',d.robot_ip);put('robot-tcp',d.tcp?.map((v,i)=>i<3?v*1000:v).join(', '),text=>vector(text,6));
        put('robot-scene',d.collision_scene);
        put('robot-workspace-min',d.workspace_min_m?.map(v=>v*1000).join(', '),text=>vector(text,3));
        put('robot-workspace-max',d.workspace_max_m?.map(v=>v*1000).join(', '),text=>vector(text,3));
        put('manual-workspace-min',d.workspace_min_m?.map(v=>v*1000).join(', '),text=>vector(text,3));
        put('manual-workspace-max',d.workspace_max_m?.map(v=>v*1000).join(', '),text=>vector(text,3));
        put('validation-workspace-min',d.workspace_min_m?.map(v=>v*1000).join(', '),text=>vector(text,3));
        put('validation-workspace-max',d.workspace_max_m?.map(v=>v*1000).join(', '),text=>vector(text,3));
        put('robot-joint-min',d.joint_bounds_rad?.map(p=>p[0]*180/Math.PI).join(', '),text=>vector(text,6));
        put('robot-joint-max',d.joint_bounds_rad?.map(p=>p[1]*180/Math.PI).join(', '),text=>vector(text,6));
        put('manual-joint-min',d.joint_bounds_rad?.map(p=>p[0]*180/Math.PI).join(', '),text=>vector(text,6));
        put('manual-joint-max',d.joint_bounds_rad?.map(p=>p[1]*180/Math.PI).join(', '),text=>vector(text,6));
        put('descent-joint-min',d.joint_bounds_rad?.map(p=>p[0]*180/Math.PI).join(', '),text=>vector(text,6));
        put('descent-joint-max',d.joint_bounds_rad?.map(p=>p[1]*180/Math.PI).join(', '),text=>vector(text,6));
        put('robot-joint-source',d.joint_bounds_source);
        put('manual-joint-source',d.joint_bounds_source);
        put('descent-joint-source',d.joint_bounds_source);
        put('robot-joint-speed',d.joint_speed_deg_s);
      }
      note.textContent='Discovered inputs prefilled from the IPC data directory or site profile. Review every value and keep physical confirmations yours.';
      document.dispatchEvent(new Event('test-profile-applied'));
    }catch(error){note.textContent=error.message;}
  };
})();
