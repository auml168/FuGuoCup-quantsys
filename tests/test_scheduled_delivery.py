import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
import zipfile
from contextlib import ExitStack, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fullgoal_quant import cli, notifications, runstatus
from fullgoal_quant.common import GuardError, TZ, read_json, write_json

ROOT=Path(__file__).resolve().parents[1]


def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class ScheduledDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        original=os.getcwd()
        directory=self.stack.enter_context(tempfile.TemporaryDirectory())
        os.chdir(directory)
        self.stack.callback(os.chdir,original)
        shutil.copytree(ROOT/'config','config',ignore=shutil.ignore_patterns('account.local.json','account.json','*.backup*'))
        self.stack.enter_context(patch.dict(os.environ,{
            'FQ_EVENT':'schedule','FQ_SCHEDULE':'20 6 * * 1-5',
            'GITHUB_RUN_ID':'100','GITHUB_ACTIONS':'true'},clear=True))
        self.at=datetime(2026,10,21,14,21,tzinfo=TZ)
        self.runner=module('cloud_run')
        self.sender=module('cloud_send')
        for target in ('fullgoal_quant.cli.now_cn','fullgoal_quant.notifications.now_cn',
                       'fullgoal_quant.runstatus.now_cn','fullgoal_quant.common.now_cn',
                       'fullgoal_quant.report.now_cn'):
            self.stack.enter_context(patch(target,side_effect=lambda:self.at))
        self.stack.enter_context(patch.object(self.sender,'now_cn',side_effect=lambda:self.at))
        # Any accidental network path fails the test before opening a socket.
        self.stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('测试禁止网络')))
        self.stack.enter_context(patch('socket.socket.connect',side_effect=AssertionError('测试禁止网络')))
        self.stack.enter_context(patch.object(cli,'extend_official',side_effect=GuardError('离线日历')))
        self.collect=self.stack.enter_context(patch.object(cli.Collector,'collect',return_value={}))
        self.evaluate=self.stack.enter_context(patch.object(cli,'evaluate',return_value={}))
        self.stack.enter_context(patch.object(cli,'load_market',return_value=({'funds':{}},{})))
        self.stack.enter_context(patch.object(cli,'analysis_codes',return_value=[]))
        self.stack.enter_context(patch.object(cli,'active_codes',return_value=[]))
        self.stack.enter_context(patch.object(cli,'metadata_for',return_value={}))
        self.builder=self.stack.enter_context(patch.object(cli,'build_report',side_effect=self.report))
        self.sleep=self.stack.enter_context(patch.object(cli,'sleep',side_effect=self.advance))
        self.telegram=self.stack.enter_context(patch.object(self.sender,'send_report'))
        self.stack.enter_context(patch.object(cli,'send_report',side_effect=AssertionError('禁止直接发送')))
        write_json('data/snapshot.json',{'funds':{}})
        write_json('reports/research/comparison.json',{})
        write_json('runtime/notification-state.json',{'version':1,'deliveries':{},'generation':0})
        Path('runtime/state-restored').touch()

    def advance(self, seconds):
        self.at+=timedelta(seconds=seconds)

    def report(self, *args, **kwargs):
        slot=kwargs['slot']
        if runstatus.production():
            self.assertTrue(cli.slot_guard(slot,self.at))
        return {'report_id':f'{self.at.date()}-{slot}','created_at':self.at.isoformat(),
                'slot':slot,'text':'离线测试报告','actionable':False,'weights':{}}

    def status(self):
        return read_json('runtime/run-status.json')['status']

    def deliver(self):
        self.assertEqual(self.runner.run(),0)
        self.assertEqual(self.status(),'reserved')
        self.assertEqual(self.sender.send(),0)
        self.assertEqual(self.status(),'sent')

    def test_on_time_schedule(self):
        self.deliver()
        self.telegram.assert_called_once_with('离线测试报告')
        self.assertEqual(notifications.delivery_status('2026-10-21-afternoon'),'sent')
        self.assertEqual(notifications.state_at()['generation'],2)

    def test_early_scheduled_dispatch_waits_before_report(self):
        self.at=self.at.replace(hour=13,minute=50)
        os.environ.update(FQ_EVENT='workflow_dispatch',FQ_MODE='scheduled',FQ_SLOT='afternoon',FQ_RESEARCH='true')
        with patch.object(self.runner,'main',wraps=cli.main) as main:
            self.deliver()
        self.assertNotIn('--research',main.call_args.args[0])
        self.sleep.assert_called_once_with(1800)
        self.assertGreaterEqual(datetime.fromisoformat(read_json('runtime/outbox.json')['created_at']).time(),cli.SLOTS['afternoon'])
        self.telegram.assert_called_once()

    def test_late_schedule_fails_without_outbox(self):
        self.at=self.at.replace(hour=21,minute=40)
        self.assertEqual(self.runner.run(),3)
        self.assertEqual(self.status(),'missed_late')
        self.assertEqual(read_json('runtime/run-status.json')['detail'],'延迟 440 分钟')
        self.assertFalse(Path('runtime/outbox.json').exists())
        self.assertEqual(self.sender.send(),1)
        self.collect.assert_not_called()
        self.telegram.assert_not_called()

    def test_early_cron_wait_excludes_collection_time(self):
        self.at=self.at.replace(hour=13,minute=50)
        os.environ['FQ_SCHEDULE']='50 5 * * 1-5'
        self.collect.side_effect=lambda *a,**kw:self.advance(8*60)
        self.deliver()
        self.sleep.assert_called_once_with(22*60)
        self.assertEqual(self.at.hour,14)
        self.assertEqual(self.at.minute,20)

    def test_late_backup_after_sent_skips(self):
        self.deliver()
        self.at=self.at.replace(hour=21,minute=40)
        os.environ['GITHUB_RUN_ID']='101'
        self.assertEqual(self.runner.run(),0)
        self.assertEqual(self.status(),'skipped_already_sent')
        self.assertFalse(Path('runtime/outbox.json').exists())
        self.assertEqual(self.sender.send(),0)
        self.telegram.assert_called_once()
        self.collect.assert_called_once()

    def test_consecutive_production_runs_send_once(self):
        self.deliver()
        os.environ['GITHUB_RUN_ID']='101'
        self.assertEqual(self.runner.run(),0)
        self.assertEqual(self.status(),'skipped_already_sent')
        self.assertEqual(self.sender.send(),0)
        self.telegram.assert_called_once()
        self.assertEqual(notifications.state_at()['generation'],2)

    def test_reserved_is_blocked(self):
        notifications.reserve(self.report(slot='afternoon'),{},owner='earlier')
        self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'blocked_unresolved')
        self.assertFalse(Path('runtime/outbox.json').exists())
        self.assertEqual(self.sender.send(),1)
        self.collect.assert_not_called()
        self.telegram.assert_not_called()

    def test_telegram_failure_is_persisted_and_never_retried(self):
        self.assertEqual(self.runner.run(),0)
        self.telegram.side_effect=GuardError('Telegram 网络失败 secret-like-content')
        self.assertEqual(self.sender.send(),1)
        self.assertEqual(self.status(),'send_failed')
        state=notifications.state_at()
        self.assertEqual(state['generation'],2)
        self.assertEqual(state['deliveries']['2026-10-21-afternoon']['reason'],'network')
        self.assertNotIn('secret-like-content',Path('runtime/notification-state.json').read_text())
        os.environ['GITHUB_RUN_ID']='101'
        self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'blocked_unresolved')
        self.assertEqual(self.sender.send(),1)
        self.telegram.assert_called_once()

    def test_report_exception_is_failed(self):
        for exc in (GuardError('生成失败'),RuntimeError('private exception')):
            with self.subTest(kind=type(exc).__name__):
                self.builder.side_effect=exc
                self.assertEqual(self.runner.run(),2)
                self.assertEqual(self.status(),'failed')
                self.assertFalse(Path('runtime/outbox.json').exists())
                self.assertEqual(self.sender.send(),1)
        self.telegram.assert_not_called()

    def test_unexpected_error_logs_class_without_exception_text(self):
        self.builder.side_effect=RuntimeError('private exception payload')
        output=io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'failed')
        self.assertIn('停止：未预期错误 RuntimeError',output.getvalue())
        self.assertNotIn('private exception payload',output.getvalue())
        self.assertNotIn('配置或数据格式异常',output.getvalue())
        self.telegram.assert_not_called()

    def test_missing_outbox_status_rules(self):
        self.assertEqual(self.sender.send(),1)
        for status in ('preview','skipped_non_session','skipped_already_sent','skipped_stale_trigger'):
            runstatus.record(status)
            self.assertEqual(self.sender.send(),0)
        for status in ('scheduled','reserved','failed','missed_late','blocked_unresolved'):
            runstatus.record(status)
            self.assertEqual(self.sender.send(),1)
        self.telegram.assert_not_called()

    def test_missing_restored_state_refuses_production(self):
        Path('runtime/state-restored').unlink()
        self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'failed')
        self.assertEqual(read_json('runtime/run-status.json')['detail'],'去重状态无法核验')
        self.collect.assert_not_called()
        self.telegram.assert_not_called()

    def test_manual_test_send_and_preview_without_state(self):
        Path('runtime/state-restored').unlink()
        self.at=self.at.replace(day=24)
        os.environ.update(FQ_EVENT='workflow_dispatch',FQ_MODE='test-send',FQ_SCHEDULE='',FQ_SLOT='')
        ids=[]
        for run_id in ('100','101'):
            os.environ['GITHUB_RUN_ID']=run_id
            self.assertEqual(self.runner.run(),0)
            outbox=read_json('runtime/outbox.json')
            self.assertTrue(outbox['test'])
            self.assertIn('-test-',outbox['report_id'])
            ids.append(outbox['report_id'])
            self.assertEqual(self.sender.send(),0)
            self.assertEqual(self.status(),'test_sent')
            self.assertIsNone(notifications.state_at().get('last_signal'))
        self.assertNotEqual(*ids)
        self.assertEqual(self.telegram.call_count,2)
        self.at=self.at.replace(day=21)
        os.environ['FQ_MODE']='preview'
        self.assertEqual(self.runner.run(),0)
        self.assertEqual(self.status(),'preview')
        self.assertFalse(Path('runtime/outbox.json').exists())
        self.assertEqual(self.sender.send(),0)
        self.assertEqual(self.telegram.call_count,2)

    def test_weekend_and_configured_holiday(self):
        holiday=read_json('config/calendar.json')['closures'][-1][0]
        for day in ('2026-10-24',holiday):
            with self.subTest(day=day):
                self.at=datetime.fromisoformat(day+'T14:21:00+08:00')
                self.assertEqual(self.runner.run(),0)
                self.assertEqual(self.status(),'skipped_non_session')
                self.assertEqual(self.sender.send(),0)
        self.collect.assert_not_called()
        self.telegram.assert_not_called()

    def test_utc_slot_and_workflow_crons(self):
        self.assertTrue(cli.slot_guard('afternoon',datetime(2026,10,21,6,20,tzinfo=timezone.utc)))
        self.assertFalse(cli.slot_guard('afternoon',datetime(2026,10,21,5,50,tzinfo=timezone.utc)))
        crons=re.findall(r"cron:\s*'([^']+)'",(ROOT/'.github/workflows/reports.yml').read_text())
        self.assertEqual(set(crons),{'50 5 * * 1-5','20 6 * * 1-5'})
        for cron in crons:
            minute,hour,_,_,weekday=cron.split()
            self.assertEqual(cli.CRONS[cron],'afternoon')
            self.assertEqual(weekday,'1-5')
            for day in range(19,26):
                at=datetime(2026,10,day,int(hour),int(minute),tzinfo=timezone.utc)
                self.assertEqual(at.weekday(),at.astimezone(TZ).weekday())

    def test_failure_notice_includes_status_without_account_amount(self):
        notice=module('failure_notice')
        amount='987654321.23'
        write_json('config/account.local.json',{'cash':amount})
        runstatus.record('missed_late',detail='延迟 331 分钟')
        with patch.object(notice,'send_report') as alert:
            self.assertEqual(notice.notice(),0)
        message=alert.call_args.args[0]
        self.assertTrue(message.startswith('今日14:20信号未送达：'))
        self.assertIn('missed_late（延迟 331 分钟）',message)
        self.assertNotIn(amount,message)
        self.assertNotIn('987654321',message)
        self.deliver()
        with patch.object(notice,'send_report') as alert:
            self.assertEqual(notice.notice(),0)
            alert.assert_not_called()
        self.assertEqual(self.runner.run(),0)
        with patch.object(notice,'send_report') as alert:
            self.assertEqual(notice.notice(),0)
            alert.assert_not_called()

    def test_too_early_and_collection_overrun_are_blocked(self):
        os.environ.update(FQ_EVENT='workflow_dispatch',FQ_MODE='scheduled',FQ_SLOT='afternoon')
        self.at=self.at.replace(hour=13,minute=39)
        self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'failed_too_early')
        self.collect.assert_not_called()
        self.at=self.at.replace(hour=14,minute=21)
        self.evaluate.side_effect=lambda *a,**kw:self.advance(35*60)
        self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'failed')
        self.builder.assert_not_called()
        self.telegram.assert_not_called()

    def test_stale_cron_skips_delivery_alert_and_heartbeat(self):
        self.at=self.at.replace(hour=1,minute=0)
        os.environ['HEALTHCHECK_PING_URL']='https://example.invalid/check'
        self.assertEqual(self.runner.run(),0)
        self.assertEqual(self.status(),'skipped_stale_trigger')
        self.assertIn('800 分钟',read_json('runtime/run-status.json')['detail'])
        self.assertEqual(self.sender.send(),0)
        self.assertFalse(Path('runtime/outbox.json').exists())
        self.collect.assert_not_called()
        self.telegram.assert_not_called()
        notice=module('failure_notice')
        heartbeat=module('heartbeat')
        with patch.object(notice,'send_report') as alert,patch.object(heartbeat.urllib.request,'urlopen') as fetch:
            self.assertEqual(notice.notice(),0)
            alert.assert_not_called()
            for job_status in ('success','failure','cancelled',''):
                os.environ['FQ_JOB_STATUS']=job_status
                self.assertEqual(heartbeat.heartbeat(),0)
            fetch.assert_not_called()

    def test_early_scheduled_dispatch_fails_and_alerts(self):
        self.at=self.at.replace(hour=1,minute=0)
        os.environ.update(FQ_EVENT='workflow_dispatch',FQ_MODE='scheduled',FQ_SLOT='afternoon')
        self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'failed_too_early')
        self.assertEqual(self.sender.send(),1)
        self.collect.assert_not_called()
        self.telegram.assert_not_called()
        notice=module('failure_notice')
        with patch.object(notice,'send_report') as alert:
            self.assertEqual(notice.notice(),0)
        self.assertTrue(alert.call_args.args[0].startswith('今日14:20信号未送达：failed_too_early'))

    def test_failure_notice_manual_prefix_and_unresolved_warning(self):
        notice=module('failure_notice')
        warning='消息可能已部分送达，请人工核对 Telegram，勿重复操作。'
        for mode in ('preview','test-send','scheduled'):
            os.environ.update(FQ_EVENT='workflow_dispatch',FQ_MODE=mode)
            for status in ('failed','blocked_unresolved','send_failed'):
                with self.subTest(mode=mode,status=status):
                    runstatus.record(status)
                    with patch.object(notice,'send_report') as alert:
                        self.assertEqual(notice.notice(),0)
                    message=alert.call_args.args[0]
                    prefix='今日14:20信号未送达' if mode=='scheduled' else '手动报告任务失败'
                    self.assertTrue(message.startswith(prefix+'：'+status))
                    self.assertIn('勿沿用旧金额',message)
                    self.assertEqual(warning in message,status in ('blocked_unresolved','send_failed'))

    def test_report_overrun_keeps_reservation_guard(self):
        def slow_report(*args,**kwargs):
            report=self.report(*args,**kwargs)
            self.advance(35*60)
            return report
        self.builder.side_effect=slow_report
        self.assertEqual(self.runner.run(),2)
        self.assertEqual(self.status(),'failed')
        self.assertFalse(Path('runtime/outbox.json').exists())
        self.assertIsNone(notifications.delivery_status('2026-10-21-afternoon'))

    def test_sending_after_cutoff_retains_reservation(self):
        self.assertEqual(self.runner.run(),0)
        self.at=self.at.replace(hour=14,minute=55,second=1)
        with self.assertRaises(SystemExit):
            self.sender.send()
        self.assertEqual(self.status(),'failed')
        self.assertEqual(notifications.delivery_status('2026-10-21-afternoon'),'reserved')
        self.telegram.assert_not_called()

    def test_slot_window_exact_boundaries(self):
        for hour,minute,second,allowed in ((14,19,59,False),(14,20,0,True),
                                           (14,55,0,True),(14,55,1,False)):
            self.assertEqual(cli.slot_guard('afternoon',self.at.replace(hour=hour,minute=minute,second=second)),allowed)

    def test_http_failure_records_only_reason_class(self):
        self.assertEqual(self.runner.run(),0)
        self.telegram.side_effect=GuardError('Telegram HTTP 403 hidden-value')
        self.assertEqual(self.sender.send(),1)
        state=notifications.state_at()
        self.assertEqual(state['deliveries']['2026-10-21-afternoon']['reason'],'http')
        self.assertNotIn('hidden-value',Path('runtime/run-status.json').read_text())

    def test_unknown_cron_and_invalid_slot_fail(self):
        for env in ({'FQ_SCHEDULE':'unknown'}, {'FQ_SCHEDULE':'30 0 * * 1-5'},
                    {'FQ_EVENT':'workflow_dispatch','FQ_MODE':'scheduled','FQ_SLOT':'invalid'}):
            with self.subTest(env=env),patch.dict(os.environ,env):
                self.assertEqual(self.runner.run(),2)
                self.assertEqual(self.status(),'failed')
        self.collect.assert_not_called()

    def test_local_late_run_still_returns_zero(self):
        self.at=self.at.replace(hour=21,minute=40)
        with patch.dict(os.environ,{'FQ_EVENT':'','FQ_MODE':''}):
            self.assertEqual(cli.main(['run','--slot','afternoon']),0)
        self.collect.assert_not_called()

    def test_status_summary_and_generation(self):
        os.environ['GITHUB_STEP_SUMMARY']='summary.md'
        self.deliver()
        self.assertIn('FQ_STATUS=sent',Path('summary.md').read_text())
        status=read_json('runtime/run-status.json')
        self.assertEqual(set(status),{'status','slot','report_id','event','run_id','at','detail'})
        self.assertEqual(status['at'],self.at.isoformat())
        notifications.fail('2026-10-21-afternoon','private text')
        self.assertEqual(notifications.state_at()['generation'],3)
        self.assertEqual(notifications.state_at()['deliveries']['2026-10-21-afternoon']['reason'],'other')

    def test_heartbeat_success_failure_skip_and_timeout(self):
        heartbeat=module('heartbeat')
        with patch.object(heartbeat.urllib.request,'urlopen') as fetch:
            self.assertEqual(heartbeat.heartbeat(),0)
            fetch.assert_not_called()
            os.environ['HEALTHCHECK_PING_URL']='https://example.invalid/check'
            os.environ['FQ_JOB_STATUS']='success'
            for status in ('sent','skipped_non_session','skipped_already_sent','failed','reserved'):
                runstatus.record(status)
                self.assertEqual(heartbeat.heartbeat(),0)
                suffix='' if status in ('sent','skipped_non_session','skipped_already_sent') else '/fail'
                fetch.assert_called_with('https://example.invalid/check'+suffix,timeout=10)
            fetch.side_effect=TimeoutError('private URL')
            self.assertEqual(heartbeat.heartbeat(),0)
            write_json('runtime/run-status.json',[])
            self.assertEqual(heartbeat.heartbeat(),0)
            fetch.reset_mock()
            os.environ.update(FQ_EVENT='workflow_dispatch',FQ_MODE='preview')
            self.assertEqual(heartbeat.heartbeat(),0)
            fetch.assert_not_called()

    def test_heartbeat_requires_successful_job_and_delivery_status(self):
        heartbeat=module('heartbeat')
        os.environ['HEALTHCHECK_PING_URL']='https://example.invalid/check'
        with patch.object(heartbeat.urllib.request,'urlopen') as fetch:
            for job_status in ('success','failure','cancelled',''):
                os.environ['FQ_JOB_STATUS']=job_status
                for status in ('sent','skipped_non_session','skipped_already_sent','failed','reserved'):
                    with self.subTest(job_status=job_status,status=status):
                        runstatus.record(status)
                        self.assertEqual(heartbeat.heartbeat(),0)
                        ok=job_status=='success' and status in ('sent','skipped_non_session','skipped_already_sent')
                        fetch.assert_called_with('https://example.invalid/check'+('' if ok else '/fail'),timeout=10)


class RestoreGenerationTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        original=os.getcwd()
        directory=self.stack.enter_context(tempfile.TemporaryDirectory())
        os.chdir(directory)
        self.stack.callback(os.chdir,original)
        self.stack.enter_context(patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/repo',
            'GITHUB_REPOSITORY_ID':'42','GITHUB_REF_NAME':'main','GITHUB_TOKEN':'offline-placeholder'},clear=True))
        self.stack.enter_context(patch('socket.socket.connect',side_effect=AssertionError('测试禁止网络')))
        self.restore=module('restore_notification_state')

    def artifact(self, number, created, final=True):
        return {'name':'notification-state-'+('final' if final else 'reserved'),
                'expired':False,'created_at':created,
                'workflow_run':{'head_branch':'main','head_repository_id':42},
                'archive_download_url':f'https://api.github.com/artifacts/{number}'}

    def archive(self, generation):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            z.writestr('notification-state.json',json.dumps({'version':1,'deliveries':{},'generation':generation}))
        return io.BytesIO(data.getvalue())

    def redirect(self, request, **kwargs):
        raise urllib.error.HTTPError(request.full_url,302,'redirect',{'Location':'https://example.invalid/signed'},None)

    def test_highest_generation_wins_across_paginated_artifacts(self):
        newest=self.artifact(1,'2026-10-21T06:30:00Z')
        older=self.artifact(2,'2026-10-21T06:20:00Z')
        irrelevant={**newest,'name':'research'}
        pages=[io.BytesIO(json.dumps({'artifacts':[newest]+[irrelevant]*99}).encode()),
               io.BytesIO(json.dumps({'artifacts':[older]}).encode())]
        opener=MagicMock()
        opener.open.side_effect=self.redirect
        with patch('urllib.request.urlopen',side_effect=pages+[self.archive(2),self.archive(5)]) as fetch, \
                patch('urllib.request.build_opener',return_value=opener):
            self.restore.restore()
        self.assertIn('page=1',fetch.call_args_list[0].args[0].full_url)
        self.assertIn('page=2',fetch.call_args_list[1].args[0].full_url)
        self.assertEqual(read_json('runtime/notification-state.json')['generation'],5)
        self.assertTrue(Path('runtime/state-restored').exists())
        for call in fetch.call_args_list[2:]:
            self.assertEqual(call.args[0],'https://example.invalid/signed')
            self.assertNotIn('headers',call.kwargs)

    def test_downloads_at_most_six_and_prefers_final_on_tie(self):
        artifacts=[self.artifact(i,f'2026-10-21T06:{20+i:02d}:00Z') for i in range(8)]
        artifacts.append(self.artifact(9,artifacts[-1]['created_at'],final=False))
        opener=MagicMock()
        opener.open.side_effect=self.redirect
        with patch('urllib.request.urlopen',side_effect=[io.BytesIO(json.dumps({'artifacts':artifacts}).encode())]+
                   [self.archive(4) for _ in range(6)]),patch('urllib.request.build_opener',return_value=opener):
            self.restore.restore()
        self.assertEqual(opener.open.call_count,6)
        self.assertEqual(opener.open.call_args_list[0].args[0].full_url,artifacts[7]['archive_download_url'])

    def test_no_artifacts_marks_missing_unless_explicit_bootstrap(self):
        for bootstrap in ('false','true'):
            with self.subTest(bootstrap=bootstrap),patch.dict(os.environ,{'FQ_ALLOW_STATE_BOOTSTRAP':bootstrap}), \
                    patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"artifacts":[]}')):
                Path('runtime').mkdir(exist_ok=True)
                Path('runtime/state-restored').touch()
                self.restore.restore()
                self.assertFalse(Path('runtime/state-restored').exists())
                self.assertEqual(Path('runtime/state-missing').exists(),bootstrap=='false')

    def test_invalid_generation_fails_closed(self):
        artifact=self.artifact(1,'2026-10-21T06:20:00Z')
        opener=MagicMock()
        opener.open.side_effect=self.redirect
        with patch('urllib.request.urlopen',side_effect=[io.BytesIO(json.dumps({'artifacts':[artifact]}).encode()),self.archive('bad')]), \
                patch('urllib.request.build_opener',return_value=opener):
            with self.assertRaises(SystemExit):
                self.restore.restore()
        self.assertFalse(Path('runtime/state-restored').exists())

    def test_restore_discards_test_status_and_outbox_before_failure(self):
        for missing_repo in (False,True):
            with self.subTest(missing_repo=missing_repo),patch.dict(os.environ):
                write_json('runtime/run-status.json',{'status':'sent'})
                write_json('runtime/outbox.json',{'report_id':'test-only'})
                if missing_repo:
                    os.environ.pop('GITHUB_REPOSITORY')
                with patch('urllib.request.urlopen',side_effect=RuntimeError('offline')):
                    with self.assertRaises(SystemExit):
                        self.restore.restore()
                self.assertFalse(Path('runtime/run-status.json').exists())
                self.assertFalse(Path('runtime/outbox.json').exists())
                self.assertFalse(Path('runtime/state-restored').exists())


if __name__=='__main__':
    unittest.main()
