"""Offline preview assets retain the HTTP and selected-model boundaries."""
import http.client
from importlib.resources import files
from pathlib import Path
import tempfile
import threading
import unittest

from anodizing_web.server import Server

WEB = files('ur_handeye').joinpath('web')

class PreviewAssetTests(unittest.TestCase):
    def test_assets_and_model_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'model.json').write_bytes(WEB.joinpath('ur10e-model.json').read_bytes())
            server=Server(0,root,robot_model='model.json')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get(path,headers=None):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=10)
                conn.request('GET',path,headers=headers or {});response=conn.getresponse()
                result=(response.status,response.getheader('Content-Type'),response.read());conn.close();return result
            try:
                for name in ('three-bundle.js','robot-three.js','THREE-LICENSE.txt'):
                    status,mime,body=get('/preview/'+name)
                    self.assertEqual(status,200);self.assertEqual(body,(WEB/name).read_bytes())
                    self.assertTrue(mime.startswith('text/'))
                self.assertEqual(get('/preview/package.json')[0],404)
                self.assertEqual(get('/preview/three-bundle.js',{'Host':'evil.example'})[0],403)
                self.assertEqual(get('/preview/three-bundle.js',{'Origin':'http://evil.example'})[0],403)
                self.assertEqual(get('/api/status',{'X-Module-Token':'wrong'})[0],403)
                self.assertIn(b'MODEL PREVIEW',get('/preview/robot-preview.js')[2])
                with (root/'model.json').open('ab') as stream:stream.write(b' ')
                self.assertEqual(get('/preview/three-bundle.js')[0],409)
            finally:
                server.shutdown();server.server_close()

    def test_nominal_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            server=Server(0,Path(tmp));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=10)
                conn.request('GET','/preview/robot-preview.js');response=conn.getresponse()
                self.assertEqual(response.status,200);self.assertIn(b'NOMINAL MODEL',response.read());conn.close()
            finally:server.shutdown();server.server_close()
