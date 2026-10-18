import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch,MagicMock
from fullgoal_quant.common import TZ,write_json

ROOT=Path(__file__).resolve().parents[1]


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result


class CloudDeliveryTests(unittest.TestCase):
    def test_restore_filters_forks_and_drops_auth_on_signed_download(self):
        restore=module('restore_notification_state')
        own={'name':'notification-state-final','expired':False,'created_at':'2026-10-19T06:21:00Z',
             'workflow_run':{'head_branch':'main','head_repository_id':42},'archive_download_url':'https://api.github.com/repos/owner/test/actions/artifacts/1/zip'}
        fork={**own,'created_at':'2026-10-20T06:21:00Z','workflow_run':{'head_branch':'main','head_repository_id':99}}
        state={'version':1,'deliveries':{'known':{'status':'reserved'}},'last_actionable':None}
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:z.writestr('notification-state.json',json.dumps(state))
        metadata=io.BytesIO(json.dumps({'artifacts':[fork,own]}).encode())
        download=io.BytesIO(data.getvalue())
        redirect=urllib.error.HTTPError(own['archive_download_url'],302,'redirect',{'Location':'https://artifacts.example/signed'},None)
        opener=MagicMock();opener.open.side_effect=redirect
        original=os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            try:
                os.chdir(d)
                with patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/test','GITHUB_REPOSITORY_ID':'42','GITHUB_REF_NAME':'main','GITHUB_TOKEN':'TEST_TOKEN_NOT_REAL'}),patch('urllib.request.urlopen',side_effect=[metadata,download]) as fetch,patch('urllib.request.build_opener',return_value=opener):
                    restore.restore()
                self.assertTrue(Path('runtime/state-restored').exists())
                self.assertEqual(json.loads(Path('runtime/notification-state.json').read_text()),state)
                self.assertEqual(fetch.call_args_list[1].args[0],'https://artifacts.example/signed')
                self.assertNotIn('headers',fetch.call_args_list[1].kwargs)
                self.assertEqual(opener.open.call_args.args[0].full_url,own['archive_download_url'])
            finally:os.chdir(original)

    def test_restore_failure_never_echoes_auth(self):
        restore=module('restore_notification_state')
        with patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/test','GITHUB_REF_NAME':'main','GITHUB_TOKEN':'TEST_SECRET','GITHUB_REPOSITORY_ID':'42'}),patch('urllib.request.urlopen',side_effect=RuntimeError('TEST_SECRET')):
            with self.assertRaises(SystemExit) as caught:restore.restore()
        self.assertNotIn('TEST_SECRET',str(caught.exception))

    def test_test_signal_never_becomes_assumed_execution(self):
        from fullgoal_quant.notifications import complete,state_at
        original=os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            try:
                os.chdir(d)
                write_json('runtime/notification-state.json',{'version':1,'deliveries':{
                    'normal':{'status':'reserved'},'test':{'status':'reserved'}},
                    'last_actionable':None,'last_signal':None})
                complete('test',report={'weights':{'100032':1.0},'test':True})
                self.assertIsNone(state_at()['last_signal'])
                complete('normal',report={'weights':{'100032':0.5}})
                self.assertEqual(state_at()['last_signal']['weights'],{'100032':0.5})
            finally:os.chdir(original)

    def test_send_requires_current_date_and_own_reservation(self):
        send=module('cloud_send')
        now=datetime(2026,10,19,14,20,tzinfo=TZ)
        original=os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            try:
                os.chdir(d)
                write_json('runtime/outbox.json',{'created_at':'2026-10-16T14:20:00+08:00','slot':'afternoon','report_id':'old','text':'not an order'})
                with patch.object(send,'now_cn',return_value=now),patch.object(send,'slot_guard',return_value=True),patch.object(send,'send_report') as telegram:
                    with self.assertRaises(SystemExit):send.send()
                    telegram.assert_not_called()
                write_json('runtime/outbox.json',{'created_at':now.isoformat(),'slot':'afternoon','report_id':'current','text':'test'})
                with patch.dict(os.environ,{'GITHUB_RUN_ID':'1'}),patch.object(send,'now_cn',return_value=now),patch.object(send,'slot_guard',return_value=True),patch.object(send,'state_at',return_value={'deliveries':{'current':{'status':'reserved','owner':'2'}}}),patch.object(send,'send_report') as telegram:
                    with self.assertRaises(SystemExit):send.send()
                    telegram.assert_not_called()
                with patch.dict(os.environ,{'GITHUB_RUN_ID':'1'}),patch.object(send,'now_cn',return_value=now),patch.object(send,'slot_guard',return_value=True),patch.object(send,'state_at',return_value={'deliveries':{'current':{'status':'reserved','owner':'1'}}}),patch.object(send,'send_report') as telegram,patch.object(send,'complete') as complete:
                    send.send();telegram.assert_called_once_with('test');complete.assert_called_once()
                    self.assertEqual(complete.call_args.args[0], 'current')
                    self.assertEqual(complete.call_args.kwargs['report']['report_id'], 'current')
            finally:os.chdir(original)


if __name__=='__main__':unittest.main()
