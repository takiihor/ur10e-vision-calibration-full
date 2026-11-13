'use strict';

// The renderer consumes the editor's existing metre and row-major frame data.
function createRobotPreview(canvas, status, fitButton) {
  let renderer, controls, camera, world, objects;
  const fail = () => { status.textContent = '3D view unavailable. You can still edit and export the scene.'; fitButton.disabled = true; };
  try {
    if (typeof THREE === 'undefined' || !THREE.OrbitControls) throw Error('Three.js unavailable');
    renderer = new THREE.WebGLRenderer({canvas, antialias:true});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    world = new THREE.Scene();
    world.background = new THREE.Color('#f3f7f9');
    camera = new THREE.PerspectiveCamera(40, 1, .001, 2000);
    camera.up.set(0, 0, 1);
    controls = new THREE.OrbitControls(camera, canvas);
    controls.minDistance = .1; controls.maxDistance = 1000;
    canvas.addEventListener('keydown', e => {
      if (e.key === 'ArrowLeft') controls.rotateLeft(.1);
      else if (e.key === 'ArrowRight') controls.rotateLeft(-.1);
      else if (e.key === 'ArrowUp') controls.rotateUp(.1);
      else if (e.key === 'ArrowDown') controls.rotateUp(-.1);
      else if (e.key === '+' || e.key === '=') controls.dollyIn(.9);
      else if (e.key === '-' || e.key === '_') controls.dollyOut(.9);
      else return;
      e.preventDefault();
    });
    controls.addEventListener('change', () => renderer.render(world, camera));
    world.add(new THREE.HemisphereLight(0xffffff, 0x758b93, 2));
    const light = new THREE.DirectionalLight(0xffffff, 1.3);
    light.position.set(2, -3, 4); world.add(light);
    objects = new THREE.Group(); world.add(objects);
    let first = true;
    const resize = () => {
      const width = canvas.clientWidth, height = canvas.clientHeight;
      if (!width || !height) return;
      renderer.setSize(width, height, false); camera.aspect = width / height;
      camera.updateProjectionMatrix();
      if (first && objects.children.length) { first = false; fit(); }
      else renderer.render(world, camera);
    };
    new ResizeObserver(resize).observe(canvas);
    canvas.addEventListener('webglcontextlost', e => { e.preventDefault(); fail(); });
    canvas.addEventListener('webglcontextrestored', () => { status.textContent = ''; fitButton.disabled = false; resize(); });
    const matrix = rows => new THREE.Matrix4().set(...rows.flat());
    const fit = () => {
      const bounds = new THREE.Box3().setFromObject(objects);
      if (bounds.isEmpty()) return;
      const centre = bounds.getCenter(new THREE.Vector3());
      const radius = Math.max(bounds.getBoundingSphere(new THREE.Sphere()).radius, .1);
      const fov = Math.min(camera.fov * Math.PI / 180, 2 * Math.atan(Math.tan(camera.fov * Math.PI / 360) * camera.aspect));
      const distance = radius / Math.sin(fov / 2) * 1.15;
      const direction = camera.position.clone().sub(controls.target);
      if (direction.lengthSq() < 1e-8) direction.set(1, -1.5, 1);
      camera.position.copy(centre).add(direction.normalize().multiplyScalar(distance));
      camera.updateProjectionMatrix(); controls.target.copy(centre); controls.update();
      renderer.render(world, camera);
    };
    fitButton.addEventListener('click', fit);
    function draw(q, scene, model, cameraVisual) {
      while (objects.children.length) {
        const o = objects.children[0]; objects.remove(o);
        o.traverse(child => { if (child.geometry) child.geometry.dispose(); if (child.material) child.material.dispose(); });
      }
      const frames = robotFrames(q, model), tool = multiply(frames.at(-1), rigid(model.tool0));
      const visual = typeof UR10E_VISUAL_MODEL !== 'undefined' && UR10E_VISUAL_MODEL.version === 1 ? UR10E_VISUAL_MODEL.bodies : [];
      model.bodies.forEach((body, i) => {
        const parts = visual?.find(item => item.name === body.name)?.parts;
        for (const part of parts?.length ? parts : [{...body, color:i%2?'#9ebccd':'#d6dfe3'}]) {
          const geometry = new THREE.BufferGeometry();
          geometry.setAttribute('position', new THREE.Float32BufferAttribute(part.vertices.flat(), 3));
          geometry.setIndex(part.faces.flat()); geometry.computeVertexNormals();
          const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({color:part.color, side:THREE.DoubleSide, flatShading:true}));
          mesh.matrixAutoUpdate = false; mesh.matrix.copy(matrix(frames[body.depth])); objects.add(mesh);
        }
      });
      for (const box of cameraVisual === undefined ? [...scene.attached, ...scene.obstacles] : scene.attached.filter(box => box.name === 'tool')) {
        const geometry = new THREE.BoxGeometry(...box.dimensions_m);
        const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({color:{tool:'#e8a558',camera:'#45a7cb',mount:'#aa8ac5'}[box.name]||'#9abb8f', flatShading:true}));
        mesh.matrixAutoUpdate = false;
        mesh.matrix.copy(matrix(scene.attached.includes(box) ? multiply(tool, rigid(box.pose)) : rigid(box.pose)));
        objects.add(mesh);
      }
      if (cameraVisual) {
        const geometry = new THREE.BoxGeometry(.09,.025,.025);
        const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({color:'#45a7cb',flatShading:true}));
        mesh.matrixAutoUpdate = false; mesh.matrix.copy(matrix(multiply(tool,cameraVisual))); objects.add(mesh);
      }
      for (const frame of [identity(), tool]) {
        const axes = new THREE.AxesHelper(.12); axes.matrixAutoUpdate = false; axes.matrix.copy(matrix(frame)); objects.add(axes);
      }
      resize();
    }
    resize();
    return draw;
  } catch (e) { fail(); return () => {}; }
}
