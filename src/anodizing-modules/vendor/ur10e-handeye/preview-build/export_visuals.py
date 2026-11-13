"""Convert the supplied Anobot/UR10e DAE visuals to portable link-frame data."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from ur_handeye.collision import transform

NS = {'c': 'http://www.collada.org/2005/11/COLLADASchema'}
LINKS = ['base_link_inertia', 'shoulder_link', 'upper_arm_link', 'forearm_link',
         'wrist_1_link', 'wrist_2_link', 'wrist_3_link']


def pose(element):
    origin = element.find('origin')
    return transform({'xyz_m': [float(v) for v in origin.get('xyz', '0 0 0').split()],
                      'rpy_rad': [float(v) for v in origin.get('rpy', '0 0 0').split()]})


def parts(path):
    root = ET.parse(path).getroot()
    assert root.find('c:asset/c:unit', NS).get('meter') == '1'
    assert not root.findall('.//c:image', NS), 'This converter expects material colours without textures.'
    geometries = {g.get('id'): g for g in root.findall('c:library_geometries/c:geometry', NS)}
    effects = {e.get('id'): e for e in root.findall('c:library_effects/c:effect', NS)}
    materials = {m.get('id'): m.find('c:instance_effect', NS).get('url')[1:]
                 for m in root.findall('c:library_materials/c:material', NS)}
    scene_id = root.find('c:scene/c:instance_visual_scene', NS).get('url')[1:]
    scene = root.find(f"c:library_visual_scenes/c:visual_scene[@id='{scene_id}']", NS)
    output = []

    def visit(node, parent):
        matrices = node.findall('c:matrix', NS)
        assert len(matrices) <= 1
        assert not any(node.findall('c:' + tag, NS) for tag in ('translate', 'rotate', 'scale'))
        local = np.eye(4) if not matrices else np.array(matrices[0].text.split(), float).reshape(4, 4)
        world = parent @ local
        for instance in node.findall('c:instance_geometry', NS):
            mesh = geometries[instance.get('url')[1:]].find('c:mesh', NS)
            bindings = {m.get('symbol'): m.get('target')[1:]
                        for m in instance.findall('c:bind_material/c:technique_common/c:instance_material', NS)}
            for primitive in mesh:
                kind = primitive.tag.rsplit('}', 1)[-1]
                if kind in ('source', 'vertices', 'lines'):
                    continue
                assert kind in ('triangles', 'polylist'), kind
                inputs = primitive.findall('c:input', NS)
                stride = max(int(i.get('offset', '0')) for i in inputs) + 1
                vertex = next(i for i in inputs if i.get('semantic') == 'VERTEX')
                reference = mesh.find(f"c:vertices[@id='{vertex.get('source')[1:]}']/c:input[@semantic='POSITION']", NS)
                source = mesh.find(f"c:source[@id='{reference.get('source')[1:]}']", NS)
                accessor = source.find('c:technique_common/c:accessor', NS)
                assert int(accessor.get('stride', '1')) == 3 and int(accessor.get('offset', '0')) == 0
                positions = np.array(source.find('c:float_array', NS).text.split(), float).reshape(-1, 3)
                assert len(positions) == int(accessor.get('count'))
                indices = np.array(primitive.find('c:p', NS).text.split(), int).reshape(-1, stride)
                indices = indices[:, int(vertex.get('offset', '0'))]
                counts = ([3] * int(primitive.get('count')) if kind == 'triangles'
                          else [int(v) for v in primitive.find('c:vcount', NS).text.split()])
                assert len(counts) == int(primitive.get('count')) and sum(counts) == len(indices)
                faces, offset = [], 0
                for count in counts:
                    assert count >= 3
                    polygon = indices[offset:offset + count]
                    faces.extend([int(polygon[0]), int(polygon[i]), int(polygon[i + 1])]
                                 for i in range(1, count - 1))
                    offset += count
                faces = np.array(faces, int)
                assert faces.size and faces.min() >= 0 and faces.max() < len(positions)
                used = np.unique(faces)
                points = positions[used] @ world[:3, :3].T + world[:3, 3]
                assert np.isfinite(points).all()
                colour = effects[materials[bindings[primitive.get('material')]]].find('.//c:diffuse/c:color', NS)
                rgba = [float(v) for v in colour.text.split()]
                assert len(rgba) == 4 and all(0 <= v <= 1 for v in rgba)
                output.append({'points': points, 'faces': np.searchsorted(used, faces).tolist(),
                               'color': '#' + ''.join(f'{round(v * 255):02x}' for v in rgba[:3])})
        for child in node.findall('c:node', NS):
            visit(child, world)

    for node in scene.findall('c:node', NS):
        visit(node, np.eye(4))
    assert output
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    urdf_path = args.assets / 'ur10e.nominal.urdf'
    urdf = ET.parse(urdf_path).getroot()
    model = json.loads(args.model.read_text())
    data = {'version': 1, 'source': 'Anobot UR10e visuals from ur_description 4.3.1',
            'source_sha256': {'ur10e.nominal.urdf': hashlib.sha256(urdf_path.read_bytes()).hexdigest()},
            'bodies': []}
    checks = []
    for depth, name in enumerate(LINKS):
        visuals, points = [], []
        for visual in urdf.find(f"link[@name='{name}']").findall('visual'):
            mesh = visual.find('geometry/mesh')
            path = args.assets / 'meshes' / Path(mesh.get('filename')).name
            data['source_sha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            scale = np.array(mesh.get('scale', '1 1 1').split(), float)
            origin = pose(visual)
            for part in parts(path):
                p = (part['points'] * scale) @ origin[:3, :3].T + origin[:3, 3]
                points.append(p)
                visuals.append({'vertices': np.round(p, 6).tolist(), 'faces': part['faces'], 'color': part['color']})
        p = np.vstack(points)
        trusted = next(body for body in model['bodies'] if body['name'] == name)
        assert trusted['depth'] == depth
        hull = np.array(trusted['vertices'])
        delta = float(np.max(np.abs(np.stack([p.min(0), p.max(0)]) - np.stack([hull.min(0), hull.max(0)]))))
        # The DAE node transforms already match the URDF collision-mesh axes.
        assert delta < .002, (name, delta)
        checks.append({'link': name, 'bounds_difference_mm': delta * 1000,
                       'triangles': sum(len(part['faces']) for part in visuals)})
        data['bodies'].append({'name': name, 'depth': depth, 'parts': visuals})
    args.output.write_text('const UR10E_VISUAL_MODEL = ' + json.dumps(data, separators=(',', ':')) +
                           ';\nif (typeof module !== "undefined") module.exports = UR10E_VISUAL_MODEL;\n')
    args.report.write_text(json.dumps({'links': checks, 'source_sha256': data['source_sha256'],
                                      'collision_model_unchanged': True,
                                      'visual_data_sha256': hashlib.sha256(args.output.read_bytes()).hexdigest()}, indent=2) + '\n')
    print(json.dumps({'links': len(checks), 'triangles': sum(c['triangles'] for c in checks),
                      'maximum_bounds_difference_mm': max(c['bounds_difference_mm'] for c in checks),
                      'output_bytes': args.output.stat().st_size}))


if __name__ == '__main__':
    main()
