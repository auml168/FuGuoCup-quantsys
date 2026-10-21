"""Optional dead-man check; never log the ping URL or fail the job."""
import os
import urllib.request
from fullgoal_quant.common import read_json
from fullgoal_quant.runstatus import production


def heartbeat():
    url=os.environ.get('HEALTHCHECK_PING_URL','')
    if not url or not production():
        print('未配置心跳或非生产调度，跳过。')
        return 0
    try:
        status=read_json('runtime/run-status.json').get('status')
    except Exception:
        status=None
    if status == 'skipped_stale_trigger':
        print('过期调度事件，不发送心跳。')
        return 0
    try:
        ok=os.environ.get('FQ_JOB_STATUS') == 'success' and status in ('sent','skipped_non_session','skipped_already_sent')
        target=url if ok else url.rstrip('/')+'/fail'
        with urllib.request.urlopen(target,timeout=10):
            pass
        print('投递状态心跳已发送。')
    except Exception:
        print('心跳请求失败，地址及异常内容已隐藏。')
    return 0


if __name__=='__main__':
    raise SystemExit(heartbeat())
