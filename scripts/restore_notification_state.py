"""Restore only trusted branch report artifacts. Never forward GitHub auth on redirects."""
import io,json,os,urllib.request,urllib.error,zipfile
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def restore():
    try:
        Path('runtime').mkdir(exist_ok=True)
        Path('runtime/run-status.json').unlink(missing_ok=True)
        Path('runtime/outbox.json').unlink(missing_ok=True)
        Path('runtime/state-restored').unlink(missing_ok=True)
        Path('runtime/state-missing').unlink(missing_ok=True)
        repo=os.environ['GITHUB_REPOSITORY']
        token=os.environ.get('GITHUB_TOKEN','')
        branch=os.environ.get('GITHUB_REF_NAME','main')
        headers={'Authorization':'Bearer '+token,'User-Agent':'FullgoalQuant','Accept':'application/vnd.github+json'}
        url='https://api.github.com/repos/'+repo+'/actions/artifacts?per_page=100'
        artifacts=[]
        for page in range(1,11):
            with urllib.request.urlopen(urllib.request.Request(url+f'&page={page}',headers=headers),timeout=25) as r:data=json.load(r)
            artifacts.extend(a for a in data['artifacts'] if a['name'].startswith('notification-state-') and not a['expired'] and a.get('workflow_run',{}).get('head_branch')==branch and a.get('workflow_run',{}).get('head_repository_id')==int(os.environ['GITHUB_REPOSITORY_ID']))
            if len(data['artifacts'])<100:
                break
        if not artifacts:
            if os.environ.get('FQ_ALLOW_STATE_BOOTSTRAP')=='true':
                print('No previous delivery state; research bootstrap only.')
                return
            Path('runtime/state-missing').write_text('missing')
            print('警告：未找到可信去重状态，禁止生产发送；请检查历史 artifact。')
            return
        candidates=sorted(artifacts,key=lambda a:(a['created_at'],a['name'].startswith('notification-state-final')),reverse=True)[:6]
        opener=urllib.request.build_opener(NoRedirect)
        best=None
        for chosen in candidates:
            try:
                opener.open(urllib.request.Request(chosen['archive_download_url'],headers=headers),timeout=25)
                raise RuntimeError('unexpected response')
            except urllib.error.HTTPError as exc:
                if exc.code!=302:raise
                signed=exc.headers['Location']
            # Signed artifact URL needs no GitHub Authorization header.
            with urllib.request.urlopen(signed,timeout=25) as r:archive=r.read(200000)
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                names=[n for n in z.namelist() if n=='notification-state.json']
                if len(names)!=1:raise ValueError('state missing')
                if z.getinfo(names[0]).file_size>200000:raise ValueError('state too large')
                candidate=json.loads(z.read(names[0]))
            if candidate.get('version')!=1 or not isinstance(candidate.get('deliveries'),dict):raise ValueError('schema')
            generation=candidate.get('generation',0)
            if type(generation) is not int or generation<0:raise ValueError('generation')
            rank=(generation,chosen['created_at'],chosen['name'].startswith('notification-state-final'))
            if best is None or rank>best:
                best,state=rank,candidate
        Path('runtime/notification-state.json').write_text(json.dumps(state))
        Path('runtime/state-restored').write_text('ok')
        print('Delivery state restored; no account data or secrets logged.')
    except Exception:
        # Fail closed: losing the dedupe store must never allow a new execution instruction.
        raise SystemExit('Cannot verify persistent delivery state; send disabled. Check Actions artifacts/permissions.') from None


if __name__=='__main__':restore()
