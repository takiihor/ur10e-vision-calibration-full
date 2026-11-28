/* Live AGV admission and one unchecked-by-default confirmation per motion control. */
(() => {
  const token=document.querySelector('meta[name="module-token"],meta[name="teach-token"]').content;
  const unknown='AGV state unknown: connect the AGV on the AGV page, or confirm it is switched off';
  const reporting='The AGV is reporting, so it is not switched off. Use AGV parked.';
  const confirmed='AGV switched off (your confirmation)';
  let state={detail:unknown};
  let active=null, accepted=null;
  const scopes=new Map();
  for(const group of document.querySelectorAll('[data-agv-approval]')){
    const scope=group.dataset.agvApproval;
    const checkbox=document.createElement('input');
    checkbox.type='checkbox';
    checkbox.autocomplete='off';
    checkbox.checked=false;
    const label=document.createElement('label');
    label.className='check confirmation agv-off-confirmation';
    label.append(checkbox,'AGV is switched off.');
    const line=document.createElement('p');
    line.id='agv-motion-'+scope;
    line.setAttribute('role','status');
    checkbox.setAttribute('aria-describedby',line.id);
    group.append(label,line);
    scopes.set(scope,{checkbox,line});
    checkbox.addEventListener('change',()=>{
      render();
      document.dispatchEvent(new CustomEvent('agv-confirmation-change',{detail:scope}));
    });
  }
  const off=scope=>scopes.get(scope)?.checkbox.checked===true;
  function reason(scope){
    if(off(scope))return state.reporting?reporting:'';
    return state.parked?'':state.detail;
  }
  function render(){
    const chosen=accepted?.job===active&&accepted?.off&&!state.reporting;
    for(const [scope,{checkbox,line}] of scopes){
      checkbox.disabled=!!active;
      const isOff=off(scope)&&!state.reporting||chosen&&accepted.scope===scope;
      const text=isOff?confirmed:reason(scope)||state.detail;
      if(line.textContent!==text)line.textContent=text;
      line.dataset.state=isOff||state.parked&&!off(scope)?'done':state.status_age_s>=3?'failed':'';
    }
    const summary=document.querySelector('[data-agv-summary]');
    if(summary){
      const text=chosen||[...scopes.keys()].some(scope=>off(scope)&&!state.reporting)?confirmed:state.detail;
      if(summary.textContent!==text)summary.textContent=text;
      summary.dataset.state=state.status_age_s>=3&&text!==confirmed?'failed':'';
    }
    document.dispatchEvent(new CustomEvent('agv-motion-status',{detail:state}));
  }
  window.agvMotion={reason,off,submitted(scope,job){
    accepted={scope,job:job.id,off:job.agv_motion==='off'};
    active=job.id;
    scopes.get(scope).checkbox.checked=false;
    render();
  }};
  async function poll(){
    try{
      const response=await window.operatorFetch('/api/agv-robot-motion',{headers:{'X-Module-Token':token}});
      if(!response.ok)throw Error();
      state=await response.json();
      active=state.active;
      if(accepted&&active!==accepted.job)accepted=null;
    }catch{state={detail:unknown};}
    render();
    setTimeout(poll,750);
  }
  render();
  void poll();
})();
