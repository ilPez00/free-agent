import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import free_agent as fa


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state.json'
        self.patcher = patch.object(fa, 'STATE_PATH', self.state)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def invoke(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = fa.main(argv)
        return rc, out.getvalue()

    def fake(self, name='fake', rc=0, text='done', role='executor'):
        return fa.Worker(name, 'fake', 1, True, 'fake login',
            lambda *args: (rc, text, ''),
            lambda r,o,e: fa.Result(r == 0 and bool(o), o),
            lambda: {'text': 'unknown'}, lambda: {'logged_in': None},
            installed=lambda: True, roles=[role])

    def test_all_planners(self):
        for name in ('claude','codex','grok','agy','kimi'):
            self.assertEqual(fa.WORKERS[name].roles, ['planner'])

    def test_executor_excludes_planners(self):
        self.assertFalse(set(w.name for w in fa.ordered(role='executor')) & {'claude','codex','grok','agy','kimi'})

    def test_agy_deferred(self):
        self.assertNotIn('agy', [w.name for w in fa.ordered(role='planner')])
        self.assertEqual(fa.ordered(['agy'], 'executor')[0].name, 'agy')

    def test_hermes_registered(self):
        self.assertIn('executor', fa.WORKERS['hermes'].roles)
        self.assertIn('orchestrator', fa.WORKERS['hermes'].roles)
        self.assertNotEqual(fa.HERMES_BIN, 'hermes')

    def test_hermes_guard(self):
        with patch.dict(os.environ, {'FREE_AGENT_DEPTH': '2'}):
            self.assertEqual(fa.hermes_run('task', self.tmp.name, 1)[0], 2)
            self.assertEqual(self.invoke(['run','task'])[0], 2)

    def test_hermes_increments(self):
        with patch.dict(os.environ, {'FREE_AGENT_DEPTH': '0'}), patch.object(fa, 'run_cmd', return_value=(0,'ok','')) as run:
            fa.hermes_run('task', self.tmp.name, 1)
            self.assertEqual(run.call_args.kwargs['env']['FREE_AGENT_DEPTH'], '1')

    def test_auth_classification(self):
        self.assertEqual(fa.classify('error: 401 invalid credential')[:2], (False,True))

    def test_quota_classification(self):
        self.assertEqual(fa.classify('error: 429 quota exceeded')[:2], (True,False))

    def test_success_not_false_quota(self):
        out = json.dumps({'type':'text', 'part':{'text':'Handle HTTP 429 quota errors'}})
        r = fa.validated_extract(fa.oc_extract, 0, out, '')
        self.assertTrue(r.ok)
        self.assertFalse(r.exhausted)

    def test_structured_error(self):
        out = json.dumps({'type':'error','error':{'message':'429 quota exceeded'}})
        r = fa.validated_extract(fa.oc_extract, 0, out, '')
        self.assertFalse(r.ok)
        self.assertTrue(r.exhausted)

    def test_empty_fails(self):
        self.assertFalse(fa.validated_extract(fa.oc_extract,0,'','').ok)

    def test_startup_json_not_reply(self):
        self.assertEqual(fa.extract_json_text(json.dumps({'type':'init','message':'starting'})), '')
        self.assertFalse(fa.validated_extract(fa._generic_extract,0,json.dumps({'type':'init'}),'').ok)

    def test_user_echo_not_reply(self):
        self.assertEqual(fa.extract_json_text(json.dumps({'type':'user','message':{'content':'task'}})), '')

    def test_codex_event(self):
        out = json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'plan'}})
        self.assertEqual(fa.extract_json_text(out), 'plan')

    def test_pi_error_zero_exit(self):
        out = json.dumps({'type':'message_end','message':{'role':'assistant','content':[],'stopReason':'error','errorMessage':'401 invalid credential'}})
        r = fa.validated_extract(fa.pi_extract,0,out,'')
        self.assertFalse(r.ok)
        self.assertTrue(r.auth_required)

    def test_private_atomic_state(self):
        fa.save_state({'workers': {}})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.state.read_text()), {'workers': {}})

    def test_reset(self):
        fa.mark({'workers': {}}, 'opencode', 'old')
        self.assertEqual(self.invoke(['reset','opencode'])[0],0)
        self.assertNotIn('opencode', fa.load_state()['workers'])

    def test_workers_json(self):
        rc,out = self.invoke(['workers','--json'])
        rows = json.loads(out)
        self.assertEqual(rc,0)
        self.assertTrue(any(r['worker']=='hermes' for r in rows))

    def test_json_fail_is_single_document(self):
        rc,out = self.invoke(['run','task','--worker','missing','--json'])
        self.assertNotEqual(rc,0)
        self.assertFalse(json.loads(out)["ok"])

    def test_mock_fallback(self):
        fail = self.fake('first', 1, '429 quota exceeded')
        ok = self.fake('second')
        with patch.object(fa,'WORKERS',{'first':fail,'second':ok}):
            rc,out = self.invoke(['run','task','--json','--no-notify','-w',self.tmp.name])
        self.assertEqual(rc,0)
        self.assertEqual(json.loads(out)['worker'],'second')
        self.assertEqual(fa.load_state()['workers']['first']['kind'],'quota')

    def test_nudge_quota_not_reauth(self):
        state={'workers':{}}
        with contextlib.redirect_stderr(io.StringIO()):
            fa.nudge_worker(fa.WORKERS['opencode'],'429',state,False,quota=True)
        before=fa.load_state()['workers']['opencode']['retry_at']
        rc,out=self.invoke(['nudge','--json','--no-notify'])
        self.assertEqual(json.loads(out)[0]['status'],'QUOTA_EXHAUSTED')
        self.assertEqual(fa.load_state()['workers']['opencode']['retry_at'],before)

    def test_unknown_login_not_inference(self):
        with patch.object(fa,'run_cmd',side_effect=AssertionError('must not run')):
            self.assertIsNone(fa.WORKERS['claude'].login()['logged_in'])
            self.assertIsNone(fa.WORKERS['cline'].login()['logged_in'])

    def test_loop_success(self):
        w=self.fake()
        with patch.object(fa,'WORKERS',{'fake':w}):
            rc,out=self.invoke(['loop','task','--worker','fake','--check','true','-w',self.tmp.name,'--no-notify'])
        self.assertEqual(rc,0)
        self.assertEqual(json.loads(out)['iterations'],1)

    def test_loop_bounded_failure(self):
        w=self.fake()
        with patch.object(fa,'WORKERS',{'fake':w}):
            rc,out=self.invoke(['loop','task','--worker','fake','--check','false','--max-iterations','2','-w',self.tmp.name,'--no-notify'])
        self.assertEqual(rc,2)
        self.assertEqual(json.loads(out)['iterations'],2)

    def test_timeout(self):
        rc,out,err=fa.run_cmd([sys.executable,'-c','import time; time.sleep(5)'],timeout=.1)
        self.assertEqual(rc,124)
        self.assertIn('timeout',err)

    def test_freepi_not_false_success(self):
        r=fa.validated_extract(fa.freepi_extract,0,'Welcome! echoed task','')
        self.assertFalse(r.ok)


if __name__=='__main__':
    unittest.main()
