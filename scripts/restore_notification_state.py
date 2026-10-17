"""Restore only trusted branch report artifacts. Never forward GitHub auth on redirects."""
import io,json,os,urllib.request,urllib.error,zipfile
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def restore():
    repo=os.environ['GITHUB_REPOSITORY']
    token=os.environ.get('GITHUB_TOKEN','')
    branch=os.environ.get('GITHUB_REF_NAME','main')
    headers={'Authorization':'Bearer '+token,'User-Agent':'FullgoalQuant','Accept':'application/vnd.github+json'}
    url='https://api.github.com/repos/'+repo+'/actions/artifacts?per_page=100'
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=25) as r:data=json.load(r)
        artifacts=[a for a in data['artifacts'] if a['name'].startswith('notification-state-') and not a['expired'] and a.get('workflow_run',{}).get('head_branch')==branch and a.get('workflow_run',{}).get('head_repository_id')==int(os.environ['GITHUB_REPOSITORY_ID'])]
        if not artifacts:
            print('No previous delivery state; research bootstrap only.')
            return
        # Prefer the final artifact when reserved/final share a timestamp.
        chosen=max(artifacts,key=lambda a:(a['created_at'],a['name'].startswith('notification-state-final')))
        opener=urllib.request.build_opener(NoRedirect)
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
            state=json.loads(z.read(names[0]))
        if state.get('version')!=1 or not isinstance(state.get('deliveries'),dict):raise ValueError('schema')
        Path('runtime').mkdir(exist_ok=True)
        Path('runtime/notification-state.json').write_text(json.dumps(state))
        Path('runtime/state-restored').write_text('ok')
        print('Delivery state restored; no account data or secrets logged.')
    except Exception:
        # Fail closed: losing the dedupe store must never allow a new execution instruction.
        raise SystemExit('Cannot verify persistent delivery state; send disabled. Check Actions artifacts/permissions.') from None


if __name__=='__main__':restore()
