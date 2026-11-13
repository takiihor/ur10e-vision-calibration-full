"""Run the real authenticated HTTP handler against only cached MCU functions."""
import ast
from email.message import Message
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
from pathlib import Path
import secrets
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlsplit

from anodizing_web import mcu

source=Path(__file__).resolve().parents[1]/'anodizing_web/server.py'
handler=next(node for node in ast.parse(source.read_text()).body if isinstance(node,ast.ClassDef) and node.name=='Handler')
namespace=dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler,json=json,secrets=secrets,urlsplit=urlsplit,MAX_BODY=32768,mcu=mcu)
exec(compile(ast.Module(body=[handler],type_ignores=[]),str(source),'exec'),namespace)


class MCUHandlerTests(unittest.TestCase):
    def request(self,method,*,token='secret',host='localhost:8870',origin=None,data=b'{}'):
        h=object.__new__(namespace['Handler'])
        h.server=SimpleNamespace(owner=object(),hosts={'localhost:8870'},token='secret')
        h.path='/api/mcu';h.headers=Message();h.headers['Host']=host
        if token is not None:h.headers['X-Module-Token']=token
        if origin is not None:h.headers['Origin']=origin
        h.headers['Content-Type']='application/json';h.headers['Content-Length']=str(len(data))
        h.rfile=BytesIO(data)
        h.reply=lambda status,body,*args,**kwargs:setattr(h,'response',(status,body))
        getattr(h,'do_'+method)()
        return h.response

    def test_authorized_get_is_cached_and_post_keeps_exact_request(self):
        body=dict(project='project',command='0',request_id='mcu-request-123',parent=None)
        with mock.patch.object(mcu,'capability',return_value={'cached_only':True}),mock.patch.object(mcu,'submit',return_value={'id':body['request_id']}) as submit:
            self.assertEqual(self.request('GET'),(200,{'cached_only':True}))
            self.assertEqual(self.request('POST',data=json.dumps(body).encode()),(202,{'id':body['request_id']}))
            self.assertEqual(submit.call_args.args[1],body)

    def test_wrong_authorization_or_malformed_body_never_reaches_mcu(self):
        with mock.patch.object(mcu,'capability') as capability,mock.patch.object(mcu,'submit') as submit:
            for method in ('GET','POST'):
                for options in ({'token':'wrong'},{'host':'evil'},{'origin':'http://evil'}):
                    with self.subTest(method=method,options=options):self.assertEqual(self.request(method,**options)[0],403)
            for body in (b'[]',b'{',b' '*32769):self.assertEqual(self.request('POST',data=body)[0],400)
            capability.assert_not_called();submit.assert_not_called()
