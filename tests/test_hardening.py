import contextlib
import io
import json
import os
from pathlib import Path
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


if __name__=='__main__': unittest.main()
