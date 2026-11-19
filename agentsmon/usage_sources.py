"""Where usage snapshots come from, and two small opt-in producers.

``discover`` maps detected agent rows to their own snapshot file:

* Codex – the session's rollout ``$CODEX_HOME/sessions/**/rollout-*-<session_id>.jsonl``
  (Codex writes ``rate_limits`` there itself after every turn).
* Claude Code – ``<state>/claude/<session_id>.json``, written by
  ``agentsmon usage claude-statusline`` used as Claude Code's status line command.
* Gemini CLI – ``<state>/gemini.json``, written by ``agentsmon usage gemini-refresh``.
  Gemini CLI keeps one login per OS user (``~/.gemini``), so every Gemini CLI row of this
  user shares that snapshot. Explicit ``sources`` mappings always take precedence.

The dashboard process itself never reads credentials or makes network requests. Only the
separately run, opt-in ``gemini-refresh`` command reads Gemini CLI's cached access token
(never refreshes or stores it) to ask Google for the remaining quota.
"""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.request

SESSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$')
GEMINI_ENDPOINT = 'https://cloudcode-pa.googleapis.com/v1internal'


def state_dir(settings=None):
    custom=(settings or {}).get('state_dir')
    if custom: return Path(custom).expanduser()
    base=os.environ.get('XDG_STATE_HOME') or str(Path.home()/'.local'/'state')
    return Path(base)/'agentsmon'/'usage'


def codex_home(settings=None):
    return Path((settings or {}).get('codex_home') or os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser()


def codex_rollout(session_id, settings=None):
    if not SESSION_RE.match(session_id or ''): return None
    home=codex_home(settings)
    hits=[]
    for sub,pattern in (('sessions','*/*/*/rollout-*-%s.jsonl'),('archived_sessions','rollout-*-%s.jsonl')):
        root=home/sub
        if root.is_dir(): hits+=root.glob(pattern%session_id)
    return max(hits,key=lambda p:p.stat().st_mtime) if hits else None


def discover(agents, settings):
    found={}
    gemini_kinds=set(settings.get('gemini_kinds') or ['gemini-cli'])
    state=state_dir(settings)
    for a in agents:
        if not isinstance(a,dict) or not a.get('name'): continue
        kind,sid=a.get('kind'),a.get('session_id')
        if kind=='codex' and sid:
            path=codex_rollout(sid,settings)
            if path: found[a['name']]={'format':'codex-rollout','path':str(path),'account':'Codex session'}
        elif kind=='claude-code' and sid and SESSION_RE.match(sid):
            path=state/'claude'/f'{sid}.json'
            if path.is_file(): found[a['name']]={'format':'claude-statusline','path':str(path),'account':'Claude session'}
        elif kind in gemini_kinds:
            path=state/'gemini.json'
            if path.is_file(): found[a['name']]={'format':'gemini-quota','path':str(path),'account':'Gemini CLI login'}
    return found


def write_json(path, data):
    """Atomic, private (0600) snapshot write."""
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w') as f: json.dump(data,f)
        os.chmod(tmp,0o600); os.replace(tmp,path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True); raise


def _short_reset(reset, now):
    left=int(reset-now)
    if left<=0: return ''
    if left<3600: return f'{left//60}m'
    if left<86400: return f'{left//3600}h{left%3600//60:02d}m'
    return f'{left//86400}d{left%86400//3600}h'


def claude_statusline(stream=None, out=None, now=None):
    """Claude Code status line command: save ``rate_limits`` per session, print a summary."""
    stream=stream or sys.stdin; out=out or sys.stdout; now=now or time.time()
    try: data=json.loads(stream.read(1024*1024))
    except ValueError: data={}
    if not isinstance(data,dict): data={}
    sid=str(data.get('session_id') or '')
    limits=data.get('rate_limits') if isinstance(data.get('rate_limits'),dict) else None
    if limits and SESSION_RE.match(sid):
        try: write_json(state_dir()/'claude'/f'{sid}.json',{'captured_at':int(now),'session_id':sid,'rate_limits':limits})
        except OSError: pass
    parts=[]
    model=(data.get('model') or {}).get('display_name') if isinstance(data.get('model'),dict) else None
    if model: parts.append(str(model))
    for label,key in (('5h','five_hour'),('7d','seven_day')):
        w=(limits or {}).get(key)
        if isinstance(w,dict) and type(w.get('used_percentage')) in (int,float):
            text=f"{label} {100-w['used_percentage']:.0f}% left"
            reset=w.get('resets_at')
            if type(reset) in (int,float) and _short_reset(reset,now): text+=f" ↻{_short_reset(reset,now)}"
            parts.append(text)
    out.write(' · '.join(parts)+'\n')
    return 0


def _post(method, token, body, timeout=15):
    req=urllib.request.Request(f'{GEMINI_ENDPOINT}:{method}',data=json.dumps(body).encode(),method='POST',
                               headers={'Authorization':'Bearer '+token,'Content-Type':'application/json',
                                        'User-Agent':'agentsmon-usage'})
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        return json.loads(resp.read(1024*1024))


def gemini_refresh(creds_path=None, out=None, now=None, post=_post):
    """Fetch Gemini CLI quota with its cached login and save ``<state>/gemini.json``."""
    out=out or sys.stdout; now=now or time.time()
    creds_path=Path(creds_path or Path.home()/'.gemini'/'oauth_creds.json')
    try: creds=json.loads(creds_path.read_text())
    except (OSError,ValueError):
        out.write(f'No Gemini CLI login found at {creds_path} (sign in with Google in Gemini CLI first).\n'); return 2
    token=creds.get('access_token'); expiry=creds.get('expiry_date')
    if not token or (type(expiry) in (int,float) and expiry/1000<=now+30):
        out.write('Gemini CLI access token is missing or expired; run Gemini CLI once to renew it.\n'); return 2
    meta={'ideType':'IDE_UNSPECIFIED','platform':'PLATFORM_UNSPECIFIED','pluginType':'GEMINI'}
    project=os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GOOGLE_CLOUD_PROJECT_ID')
    try:
        load=post('loadCodeAssist',token,{'cloudaicompanionProject':project,'metadata':{**meta,'duetProject':project}})
        project=load.get('cloudaicompanionProject') or project
        if isinstance(project,dict): project=project.get('id')
        if not project:
            out.write('Gemini account has no Code Assist project; quota is unavailable.\n'); return 2
        quota=post('retrieveUserQuota',token,{'project':project})
    except Exception as exc:
        out.write(f'Gemini quota request failed: {type(exc).__name__}\n'); return 1
    buckets=[b for b in quota.get('buckets') or [] if isinstance(b,dict)]
    write_json(state_dir()/'gemini.json',{'captured_at':int(now),'buckets':buckets})
    out.write(f'Saved {len(buckets)} Gemini quota bucket(s) to {state_dir()/"gemini.json"}\n')
    return 0
