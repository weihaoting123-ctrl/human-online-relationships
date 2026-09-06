import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.run_local_pipeline import run_pipeline
from dashboard.modules import ModuleDisabledError


class Policy:
    def __init__(self, disabled=()):
        self.disabled = set(disabled)
    def require(self, module):
        if module in self.disabled:
            raise ModuleDisabledError('disabled')


class PipelineTests(unittest.TestCase):
    def test_disabled_modules_skip_without_deleting_and_refresh_before_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def runner(command, **kwargs):
                calls.append(Path(command[3]).name)
                self.assertTrue(kwargs['capture_output'])
                return SimpleNamespace(returncode=0)
            result = run_pipeline(Path(temp), registry=Policy({'sync','voice'}), runner=runner)
            self.assertEqual(calls,['archive_wechat_local.py','refresh_library.py','backup_wechat_local.py'])
            self.assertEqual(result['status'],'ok')
            self.assertEqual(sum(item['state']=='skipped' for item in result['steps']),4)

    def test_failure_is_not_success_and_backup_is_still_attempted(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def runner(command, **kwargs):
                name = Path(command[3]).name
                calls.append(name)
                return SimpleNamespace(returncode=1 if name=='sync_all_wechat.py' else 0)
            result = run_pipeline(Path(temp),registry=Policy(),runner=runner)
            self.assertEqual(result['status'],'error')
            self.assertEqual(calls[-1],'backup_wechat_local.py')

    def test_corrupt_policy_fails_closed_and_does_not_execute(self):
        class Broken:
            def require(self,_module):
                raise RuntimeError('secret path must not be returned')
        with tempfile.TemporaryDirectory() as temp:
            calls=[]
            result=run_pipeline(Path(temp),registry=Broken(),runner=lambda *a,**k:calls.append(a))
            self.assertEqual(result['status'],'error')
            self.assertEqual(calls,[])
            self.assertNotIn('secret',str(result))

    def test_conflicting_data_override_cannot_bypass_disabled_module_settings(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict('os.environ',{'SHE_LOVE_ME_DATA_DIR':str(Path(temp)/'custom')}):
            calls=[]
            def runner(*args,**kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=0)
            result=run_pipeline(Path(temp),registry=Policy(),runner=runner)
            self.assertEqual(result['status'],'error')
            self.assertEqual(result['error_code'],'DATA_ROOT_MISMATCH')
            self.assertEqual(calls,[])


if __name__=='__main__':
    unittest.main()
