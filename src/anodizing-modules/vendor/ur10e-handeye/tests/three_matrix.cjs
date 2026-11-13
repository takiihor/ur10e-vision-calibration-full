'use strict';
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const web=path.join(__dirname,'../src/ur_handeye/web');
const context={console};vm.createContext(context);vm.runInContext(fs.readFileSync(web+'/three-bundle.js','utf8'),context);
const {THREE}=context,{multiply,rigid,point,robotFrames}=require(web+'/robot-preview.js');
const model=require(web+'/ur10e-model.js').UR10E_MODEL;
for(const q of [[0,0,0,0,0,0],[.2,-1.9,-1.6,-1.2,1.57,.8]]){
  const frames=robotFrames(q,model);
  for(const transform of [...frames,multiply(frames.at(-1),rigid(model.tool0))]){
    const matrix=new THREE.Matrix4().set(...transform.flat());
    for(const p of [[0,0,0],[.1,-.3,.5]]){
      const expected=point(transform,p),actual=new THREE.Vector3(...p).applyMatrix4(matrix).toArray();
      expected.forEach((x,i)=>assert.ok(Math.abs(x-actual[i])<1e-12));
    }
  }
}
console.log('Three.js row-major matrix and metre transforms: PASS');
