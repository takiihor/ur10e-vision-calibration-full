/* Saving uses stopped joint feedback only. */
(() => {
  const el=id=>document.getElementById(id);
  const confirm=el('travel-pose-confirmed'), save=el('travel-pose-save');
  let available=false, active=false, saving=false;
  function render(){
    const why=saving?'Reading robot joints…':active?'Wait for the current job to finish.':
      !available?'Robot feedback access is unavailable.':!confirm.checked?'Confirm the folded pose to save it.':'';
    save.disabled=!!why;
    save.title=why;
    el('travel-pose-reason').textContent=why;
  }
  async function refresh(){
    try{
      const travel=await moduleRequest('/api/agv-travel-pose');
      const pose=travel.pose;
      available=travel.robot_configured;
      el('travel-pose-value').textContent=pose?
        'Saved '+new Date(pose.saved_at).toLocaleString()+' · joints (°): '+pose.joints_rad.map(v=>(v*180/Math.PI).toFixed(1)).join(', '):
        'No AGV travel pose saved.';
      el('travel-pose-live').textContent=travel.detail;
    }catch{
      available=false;
      el('travel-pose-live').textContent='Travel pose unavailable. Check the service connection.';
    }
    render();
  }
  confirm.checked=false;
  confirm.addEventListener('change',render);
  document.addEventListener('manual-owner-status',event=>{active=!!event.detail?.active;render();});
  save.addEventListener('click',async()=>{
    if(save.disabled)return;
    saving=true;render();
    const outcome=el('travel-pose-outcome');
    outcome.textContent='Reading robot joints…';
    try{
      const pose=await moduleRequest('/api/agv-travel-pose',{project:el('project').value.trim(),confirmed:true});
      confirm.checked=false;
      outcome.textContent='Travel pose saved at '+new Date(pose.saved_at).toLocaleTimeString()+'.';
      await refresh();
    }catch(error){outcome.textContent=error.message;}
    finally{saving=false;render();}
  });
  void refresh();
})();
