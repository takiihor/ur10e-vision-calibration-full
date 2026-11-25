'use strict';
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const web=path.join(__dirname,'../src/ur_handeye/web');
const context={console}; vm.createContext(context);
vm.runInContext(fs.readFileSync(web+'/three-bundle.js','utf8'),context);
const three=context.THREE, frames=require(web+'/robot-preview.js');
let world;
class Renderer { setPixelRatio() {} setSize() {} render(scene) { world=scene; } }
class Controls {
  constructor() { this.target=new three.Vector3(); }
  addEventListener() {} update() {} rotateLeft() {} rotateUp() {} dollyIn() {} dollyOut() {}
}
Object.assign(context,frames,{
  THREE:{...three,WebGLRenderer:Renderer,OrbitControls:Controls},
  identity:()=>[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
  window:{devicePixelRatio:1}, ResizeObserver:class { observe() {} }
});
vm.runInContext(fs.readFileSync(web+'/robot-three.js','utf8'),context);
const canvas={clientWidth:800,clientHeight:600,addEventListener(){}}, button={addEventListener(){}};
const draw=context.createRobotPreview(canvas,{},button);
const q=[.2,-1.9,-1.6,-1.2,1.57,.8];
const pose={xyz_m:[.1,.2,.3],rpy_rad:[0,0,0]};
const box=name=>({name,dimensions_m:[.1,.2,.3],pose});
const scene={attached:['tool','camera','mount'].map(box),obstacles:[box('cell')]};
const part={vertices:[[.1,.2,.3],[.4,.2,.3],[.1,.5,.3]],faces:[[0,1,2]],color:'#336699'};
const visual={version:1,source:'test',bodies:[{name:'upper_arm_link',depth:6,parts:[part,{...part,color:'#995533'}]}]};
const nominal = require(web+'/ur10e-model.js').UR10E_MODEL;
const calibrated = structuredClone(nominal);
calibrated.nominal = false;
calibrated.joints[0].xyz_m[2] += .001;
const models = [nominal, calibrated];
const meshes=()=>world.children.find(child=>child.isGroup).children.filter(child=>child.isMesh);
function matrixIs(mesh,rows) {
  const expected=new three.Matrix4().set(...rows.flat());
  mesh.matrix.elements.forEach((x,i)=>assert.ok(Math.abs(x-expected.elements[i])<1e-12));
}
for(const model of models) {
  const original=JSON.stringify(model), index=model.bodies.findIndex(body=>body.name==='upper_arm_link');
  context.UR10E_VISUAL_MODEL=visual; draw(q,scene,model);
  let items=meshes(), jointFrames=frames.robotFrames(q,model);
  assert.equal(items.length,model.bodies.length+1+4);
  for(const [offset,color] of [[0,'336699'],[1,'995533']]) {
    const mesh=items[index+offset];
    matrixIs(mesh,jointFrames[model.bodies[index].depth]);
    assert.deepEqual(Array.from(mesh.geometry.index.array),[0,1,2]);
    assert.ok(Math.abs(mesh.geometry.attributes.position.array[0]-.1)<1e-7);
    assert.equal(mesh.material.color.getHexString(),color);
    assert.equal(mesh.material.flatShading,true);
  }
  for(const [offset,color] of ['e8a558','45a7cb','aa8ac5','9abb8f'].entries()) {
    assert.equal(items[model.bodies.length+1+offset].material.color.getHexString(),color);
  }
  matrixIs(items[model.bodies.length+1],frames.multiply(frames.multiply(jointFrames.at(-1),frames.rigid(model.tool0)),frames.rigid(pose)));
  matrixIs(items.at(-1),frames.rigid(pose));
  delete context.UR10E_VISUAL_MODEL; draw(q,scene,model); items=meshes();
  assert.equal(items.length,model.bodies.length+4);
  matrixIs(items[index],jointFrames[model.bodies[index].depth]);
  assert.equal(items[index].geometry.attributes.position.count,model.bodies[index].vertices.length);
  assert.equal(items[index].material.flatShading,true);
  assert.equal(JSON.stringify(model),original);
}
const calibration=[[0,-1,0,.12],[1,0,0,-.07],[0,0,1,.23],[0,0,0,1]];
context.UR10E_VISUAL_MODEL=visual;
for(const model of models){
  draw(q,scene,model,null);let items=meshes();assert.equal(items.length,model.bodies.length+2);
  draw(q,scene,model,calibration);items=meshes();assert.equal(items.length,model.bodies.length+3);
  const flange=frames.multiply(frames.robotFrames(q,model).at(-1),frames.rigid(model.tool0));
  matrixIs(items.at(-1),frames.multiply(flange,calibration));
  const size=items.at(-1).geometry.parameters;
  assert.equal(size.width,.09);assert.equal(size.height,.025);assert.equal(size.depth,.025);
  assert.equal(items.at(-1).material.color.getHexString(),'45a7cb');
}
console.log('Renderer full scene and visual-only camera matrix: PASS');
