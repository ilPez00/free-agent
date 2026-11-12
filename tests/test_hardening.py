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
import profiles
import workflows


class HardeningTests(unittest.TestCase):
    def test_credential_ignores_blank_duplicate_without_changing_dotenv_semantics(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            path = Path(root)/'keys.env'
            path.write_text('NVIDIA_API_KEY=valid-selected-key\nNVIDIA_API_KEY= # generated placeholder\nG4F_NVIDIA=unrelated-setting\n')
            self.assertEqual(profiles.read_env_file(path)['NVIDIA_API_KEY'], '')
            self.assertEqual(profiles.credential_for('nvidia', path, {}), 'valid-selected-key')

    def test_lenient_shared_env_does_not_run_shell(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            path=Path(root)/'keys.env'
            path.write_text('=== header ===\nsource /not/a/real/file\nNVIDIA_API_KEY=good\nBAD=$(touch NEVER)\n')
            self.assertEqual(profiles.credential_for('nvidia',path,{}),'good')
            self.assertFalse((Path(root)/'NEVER').exists())

    def test_g4f_setting_not_nvidia_key(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            path=Path(root)/'keys.env';path.write_text('G4F_NVIDIA=model-name\n')
            self.assertIsNone(profiles.credential_for('nvidia',path,{}))

    def test_replay_requires_zero_provider_receipt(self):
        for calls in (None, 1, False):
            receipt={'status':'succeeded','operation':'replay','provider_calls':calls}
            status,_,_=workflows._native_result(0,json.dumps(receipt),'',False)
            self.assertEqual(status,'blocked')

    def test_role_from_markdown_without_agents(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            config=Path(root)/'agents.json';prompt=Path(root)/'role.md';prompt.write_text('Read only. Task: {task}')
            with patch.dict(os.environ,{'FREE_AGENT_CONFIG':str(config),'FREE_AGENT_ENV_FILE':str(Path(root)/'empty.env')}), contextlib.redirect_stdout(io.StringIO()):
                rc=fa.main(['roles','set','reviewer','--prompt-file',str(prompt),'--json'])
            self.assertEqual(rc,0)
            self.assertEqual(profiles.load_config(config)['agents'],[])
            self.assertEqual(profiles.role_prompt(profiles.load_config(config),'reviewer','hello'),'Read only. Task: hello')

    def test_wrapper_result_is_parsed_but_user_echo_is_not(self):
        self.assertEqual(fa.extract_json_text(json.dumps({'ok':True,'text':'DONE'})),'DONE')
        self.assertEqual(fa.extract_json_text(json.dumps({'type':'user','ok':True,'text':'echo'})),'')

    def test_pretty_printed_provider_wrapper_response(self):
        out = json.dumps({'ok': True, 'text': 'PROFILE_OK', 'model': 'demo'}, indent=2)
        result = fa.validated_extract(fa._generic_extract, 0, out, '')
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'PROFILE_OK')

    def test_empty_config_can_be_repaired(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            path = Path(root)/'agents.json'; path.write_text(json.dumps({'version':1,'agents':[],'roles':{}}))
            with patch.dict(os.environ, {'FREE_AGENT_CONFIG':str(path),'FREE_AGENT_ENV_FILE':str(Path(root)/'empty.env')}), contextlib.redirect_stdout(io.StringIO()):
                rc=fa.main(['agent','add','demo','--command-json','["printf","{task}"]','--role','executor','--json'])
            self.assertEqual(rc,0)
            self.assertEqual(profiles.load_config(path)['agents'][0]['name'],'demo')

    def test_invalid_role_template_rejected_at_load(self):
        cfg={'version':1,'agents':[],'roles':{'reviewer':{'prompt':'bad {untrusted}'}}}
        with self.assertRaises(ValueError): profiles.validate_config(cfg)


class WorkdirIsVisibleToTheChild(unittest.TestCase):
    def test_run_cmd_sets_pwd_to_the_requested_workdir(self):
        # `cwd=` alone left PWD stale, so a CLI that trusts PWD (opencode) worked
        # in the caller's repository while reporting success.
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            rc, out, err = fa.run_cmd(
                [sys.executable, '-c', 'import os; print(os.getcwd()); print(os.environ.get("PWD"))'],
                cwd=root, timeout=15)
            self.assertEqual(rc, 0, err)
            cwd_line, pwd_line = out.strip().splitlines()
            self.assertEqual(os.path.realpath(cwd_line), os.path.realpath(root))
            self.assertEqual(os.path.realpath(pwd_line), os.path.realpath(root))

    def test_run_cmd_keeps_a_caller_supplied_env(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as root:
            rc, out, _ = fa.run_cmd(
                [sys.executable, '-c', 'import os; print(os.environ.get("KEEP")); print(os.environ.get("PWD"))'],
                cwd=root, timeout=15, env={'KEEP': 'yes', 'PATH': os.environ.get('PATH', '')})
            self.assertEqual(rc, 0)
            kept, pwd_line = out.strip().splitlines()
            self.assertEqual(kept, 'yes')
            self.assertEqual(os.path.realpath(pwd_line), os.path.realpath(root))


class ProviderConfigDiagnostics(unittest.TestCase):
    def test_unknown_provider_key_is_named_in_the_error(self):
        cfg = {'version': 1, 'agents': [], 'roles': {},
               'providers': {'nvidia': {'model': 'm', 'model_order': ['m']}}}
        with self.assertRaises(ValueError) as ctx:
            profiles.validate_config(cfg)
        message = str(ctx.exception)
        self.assertIn('nvidia', message)
        self.assertIn('model_order', message)
        self.assertIn('account_id', message)

    def test_empty_account_id_is_treated_as_absent(self):
        cfg = {'version': 1, 'agents': [], 'roles': {},
               'providers': {'nvidia': {'account_id': '', 'model': 'm'}}}
        clean = profiles.validate_config(cfg)
        self.assertIsNone(clean['providers']['nvidia']['account_id'])
        self.assertEqual(clean['providers']['nvidia']['model'], 'm')

    def test_a_real_account_id_is_still_validated(self):
        cfg = {'version': 1, 'agents': [], 'roles': {},
               'providers': {'cloudflare': {'account_id': 'short', 'model': 'm'}}}
        with self.assertRaises(ValueError) as ctx:
            profiles.validate_config(cfg)
        self.assertIn('32-character', str(ctx.exception))


if __name__=='__main__': unittest.main()
