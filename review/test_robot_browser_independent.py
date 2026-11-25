"""Headless Firefox through real owner with fake robot/checker only."""
from unittest import mock
from test_manual_connected_browser import ConnectedManualBrowser
import test_manual_execution

class RobotBrowser(ConnectedManualBrowser):
    def test_all_pages_and_calibration_step_approval_refresh(self):
        from anodizing_web import robot_control
        h=test_manual_execution.ManualWorkerTests();owner=h.public_server();self.addCleanup(h.doCleanups)
        patch=mock.patch.object(robot_control,'native_backend',return_value=(h.robot,h.robot));patch.start();self.addCleanup(patch.stop)
        self.open_browser(h,'/calibration')
        for path in ('/calibration','/teaching','/pick-place'):
            self.call('POST','/session/'+self.session+'/url',{'url':'http://127.0.0.1:'+str(h.server.server_port)+path})
            self.wait(lambda:self.js("return !!document.getElementById('robot-card')"),'robot card')
            result=self.js('''const c=document.querySelector('#robot-card'),m=document.querySelector('.step-content'),o=document.querySelector('#current-operation');
              const ids=[...document.querySelectorAll('[id]')].map(x=>x.id);
              return {main:m.contains(c),axes:c.querySelectorAll('[data-robot-axis]').length,unique:ids.length===new Set(ids).size,
              confirmation:o.contains(document.querySelector('#manual-reply-confirmed')),
              approve:o.contains(document.querySelector('#manual-reply')),stop:o.contains(document.querySelector('#manual-stop')),
              defaults:[document.querySelector('#robot-translation-step').value,document.querySelector('#robot-rotation-step').value,document.querySelector('#robot-joint-speed').value]};''')
            self.assertEqual(result,{'main':True,'axes':12,'unique':True,'confirmation':True,'approve':True,'stop':True,'defaults':['5','1','3']})
        self.call('POST','/session/'+self.session+'/url',{'url':'http://127.0.0.1:'+str(h.server.server_port)+'/calibration'})
        self.wait(lambda:self.js("return !!document.getElementById('robot-card')"),'calibration boot')
        self.js('''document.querySelector('#robot-card').open=true;
          for(const [id,v] of Object.entries({'robot-ip':'fake','robot-tcp':'0,0,150,0,0,0','robot-scene':'reviewed-scene.json',
          'robot-workspace-min':'-820,-840,25','robot-workspace-max':'820,840,1220',
          'robot-joint-min':'-360,-360,-360,-360,-360,-360','robot-joint-max':'360,360,360,360,360,360','robot-joint-source':'fixture'})){
            const e=document.getElementById(id);e.value=v;e.dispatchEvent(new Event('input'));}''')
        self.wait(lambda:self.js("return !document.querySelector('[data-robot-axis=x][data-robot-direction=\\\"1\\\"]').disabled"),'step ready')
        self.js("document.querySelector('[data-robot-axis=x][data-robot-direction=\\\"1\\\"]').click()")
        self.wait(lambda:self.js("return document.querySelector('#manual-job-status').dataset.stage==='awaiting_plan'"),'plan pending')
        self.assertEqual(h.robot.moves,[])
        self.refresh();self.wait(lambda:self.js("return document.querySelector('#manual-job-status').dataset.stage==='awaiting_plan'"),'plan after refresh')
        self.assertEqual(h.robot.moves,[])
        self.assertTrue(self.js("return document.querySelector('#manual-reply').disabled"))
        self.js("document.querySelector('#manual-reply-confirmed').click()")
        self.wait(lambda:self.js("return !document.querySelector('#manual-reply').disabled"),'confirmation enabled')
        self.js("document.querySelector('#manual-reply').click();document.querySelector('#manual-reply').click()")
        self.wait(lambda:owner.active is None,'fake step complete')
        self.assertEqual(len(h.robot.moves),1)
        self.refresh();self.assertEqual(len(h.robot.moves),1)

    def test_failure_and_accepted_request_remain_blocked_until_inspection(self):
        h=test_manual_execution.ManualWorkerTests();h.public_server();self.addCleanup(h.doCleanups)
        self.open_browser(h,'/calibration')
        failures=[]
        for mode in ('failure','accepted'):
            self.call('POST','/session/'+self.session+'/url',{'url':'http://127.0.0.1:'+str(h.server.server_port)+'/calibration'})
            self.wait(lambda:self.js("return !!document.getElementById('robot-card')"),'robot card')
            self.js('''document.querySelector('#robot-card').open=true;
              for(const [id,v] of Object.entries({'robot-ip':'fake','robot-tcp':'0,0,150,0,0,0','robot-scene':'scene.json',
              'robot-workspace-min':'-820,-840,25','robot-workspace-max':'820,840,1220',
              'robot-joint-min':'-360,-360,-360,-360,-360,-360','robot-joint-max':'360,360,360,360,360,360','robot-joint-source':'fixture'})){
                const e=document.getElementById(id);e.value=v;e.dispatchEvent(new Event('input'));}
              window.stepCalls=0;window.startManualJob=async()=>{window.stepCalls++;if(arguments[0]==='failure')throw Error('fixture request error');return {id:'accepted-fixture'};};''',mode)
            self.wait(lambda:self.js("return ![...document.querySelectorAll('[data-robot-axis]')][0].disabled"),'step ready')
            self.js("document.querySelector('[data-robot-axis]').click()")
            self.wait(lambda:self.js("return window.stepCalls===1"),'one submission')
            self.wait(lambda:self.js("return !document.querySelector('#robot-control-message').textContent.includes('progress')"),'posting settled')
            status=self.js("return {disabled:[...document.querySelectorAll('[data-robot-axis]')].every(x=>x.disabled),message:document.querySelector('#robot-control-message').textContent};")
            self.js("document.querySelector('[data-robot-axis]').click()")
            calls=self.js('return window.stepCalls')
            if not status['disabled'] or (mode=='failure' and 'fixture request error' not in status['message']) or calls!=1:failures.append((mode,status,calls))
        self.assertFalse(failures,failures)

    def test_definitive_400_allows_correction_then_new_explicit_click(self):
        h=test_manual_execution.ManualWorkerTests();h.public_server();self.addCleanup(h.doCleanups)
        self.open_browser(h,'/calibration')
        self.wait(lambda:self.js("return !!document.getElementById('robot-card')"),'robot card')
        self.js('''document.querySelector('#robot-card').open=true;
          for(const [id,v] of Object.entries({'robot-ip':'fake','robot-tcp':'0,0,150,0,0,0','robot-scene':'bad-scene.json',
          'robot-workspace-min':'-820,-840,25','robot-workspace-max':'820,840,1220',
          'robot-joint-min':'-360,-360,-360,-360,-360,-360','robot-joint-max':'360,360,360,360,360,360','robot-joint-source':'fixture'})){
            const e=document.getElementById(id);e.value=v;e.dispatchEvent(new Event('input'));}
          window.stepCalls=0;const real=window.fetch.bind(window);window.fetch=(url,opt)=>{
            if(url==='/api/robot-control'&&opt?.method==='POST'){window.stepCalls++;return Promise.resolve(new Response(JSON.stringify({error:'Invalid scene fixture'}),{status:400,headers:{'Content-Type':'application/json'}}));}
            return real(url,opt);};''')
        self.wait(lambda:self.js("return !document.querySelector('[data-robot-axis]').disabled"),'step ready')
        self.js("document.querySelector('[data-robot-axis]').click()")
        self.wait(lambda:self.js("return document.querySelector('#robot-control-message').textContent.includes('Invalid scene fixture')"),'rejection shown')
        self.assertEqual(self.js('return window.stepCalls'),1)
        self.js("const e=document.querySelector('#robot-scene');e.value='fixed-scene.json';e.dispatchEvent(new Event('input'))")
        self.assertFalse(self.js("return document.querySelector('[data-robot-axis]').disabled"),'corrected rejected request must allow an explicit new click')
        self.assertEqual(self.js('return window.stepCalls'),1)
        self.js("document.querySelector('[data-robot-axis]').click()")
        self.wait(lambda:self.js('return window.stepCalls===2'),'new explicit submission')

    def test_settings_edit_immediately_disables_existing_confirmation(self):
        h=test_manual_execution.ManualWorkerTests();h.public_server();self.addCleanup(h.doCleanups)
        self.open_browser(h,'/calibration')
        self.wait(lambda:self.js("return !!document.getElementById('robot-card')"),'robot card')
        state=self.js('''const c=document.querySelector('#manual-reply-confirmed'),b=document.querySelector('#manual-reply');
          c.checked=true;b.disabled=false;const e=document.querySelector('#robot-scene');e.value='different.json';e.dispatchEvent(new Event('input'));
          return {checked:c.checked,disabled:b.disabled};''')
        self.assertEqual(state,{'checked':False,'disabled':True})

    def test_capture_review_screenshots(self):
        import base64
        from pathlib import Path
        from anodizing_web import robot_control
        h=test_manual_execution.ManualWorkerTests();owner=h.public_server();self.addCleanup(h.doCleanups)
        patch=mock.patch.object(robot_control,'native_backend',return_value=(h.robot,h.robot));patch.start();self.addCleanup(patch.stop)
        self.open_browser(h,'/calibration')
        self.call('POST','/session/'+self.session+'/window/rect',{'width':1280,'height':720})
        self.wait(lambda:self.js("return !!document.querySelector('#camera_provider')"),'camera provider')
        self.js("const x=document.querySelector('#camera_provider');x.value='basler';x.dispatchEvent(new Event('change'))")
        out=Path(__file__).parent/'results'
        (out/'basler-setup-offline.png').write_bytes(base64.b64decode(self.call('GET','/session/'+self.session+'/screenshot')))
        self.call('POST','/session/'+self.session+'/url',{'url':'http://127.0.0.1:'+str(h.server.server_port)+'/pick-place'})
        self.wait(lambda:self.js("return !!document.querySelector('#robot-card')"),'robot card')
        self.js('''document.querySelector('#robot-card').open=true;
          for(const [id,v] of Object.entries({'robot-ip':'fake','robot-tcp':'0,0,150,0,0,0','robot-scene':'reviewed-scene.json',
          'robot-workspace-min':'-820,-840,25','robot-workspace-max':'820,840,1220',
          'robot-joint-min':'-360,-360,-360,-360,-360,-360','robot-joint-max':'360,360,360,360,360,360','robot-joint-source':'fixture'})){
            const e=document.getElementById(id);e.value=v;e.dispatchEvent(new Event('input'));}''')
        self.wait(lambda:self.js("return !document.querySelector('[data-robot-axis]').disabled"),'step ready')
        self.js("document.querySelector('[data-robot-axis]').click()")
        self.wait(lambda:self.js("return document.querySelector('#manual-job-status').dataset.stage==='awaiting_plan'"),'fake plan')
        self.assertIn('Joint speed 3.00 degrees/s',self.js("return document.querySelector('#manual-route-summary').textContent"))
        self.js("document.querySelector('#robot-control-message').textContent='Inspect the accepted step in Current operation before another request.';document.querySelector('#robot-card').scrollIntoView({block:'start'})")
        (out/'robot-step-approval-offline.png').write_bytes(base64.b64decode(self.call('GET','/session/'+self.session+'/screenshot')))
        self.assertEqual(h.robot.moves,[])
