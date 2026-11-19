"""Opt-in, account-scoped usage column. Inspired by TomasErnest's PR #8.

Read local usage/status-line snapshots only. No OAuth token reads or network
requests; never infer that all agents from one vendor share an account.

Each bar carries the used percentage, the remaining percentage and, when the
source reports it, the reset time (epoch seconds) so the dashboard can show
"77% left · resets in 2h 05m". With ``"auto": true`` the plugin finds the
per-session snapshot of each detected agent itself (see ``usage_sources``).
"""
from datetime import datetime
import json
import math
from pathlib import Path
import time

from . import usage_sources

MAX_BYTES = 2_000_000
STALE_SECONDS = 600


def epoch(value):
    if type(value) in (int,float) and math.isfinite(value):
        # Some producers report milliseconds.
        return int(value/1000) if value>1e11 else int(value)
    if isinstance(value,str) and value:
        return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp())
    return None


def window(label, pct, reset, now):
    if type(pct) not in (int,float) or not math.isfinite(pct) or not 0<=pct<=100: return None
    reset=epoch(reset)
    if reset and reset<=now: return None
    bar={'label':label,'percent':round(pct,1),'remaining':round(100-pct,1),
         'title':('Resets '+datetime.fromtimestamp(reset).astimezone().isoformat(timespec='minutes')) if reset
                 else 'Reset time unavailable'}
    if reset: bar['reset']=reset
    return bar


def _label(w, fallback):
    # Prefer reported window length, not a guessed five-hour/week label.
    minutes=w.get('window_minutes') or (w.get('limit_window_seconds') or 0)/60
    if type(minutes) in (int,float) and minutes>0:
        return f'{minutes/1440:g}d' if minutes%1440==0 else f'{minutes/60:g}h'
    return fallback


def _read(path, tail=False):
    with path.open('rb') as stream:
        if tail: stream.seek(max(0,stream.seek(0,2)-MAX_BYTES))
        raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES: raise ValueError('Oversized snapshot')
    return raw


def _codex_limits(raw):
    for line in reversed(raw.splitlines()):
        try: record=json.loads(line)
        except ValueError: continue
        if not isinstance(record,dict): continue
        payload=record.get('payload') or {}
        limits=payload.get('rate_limits') if isinstance(payload,dict) else None
        if limits: return limits,epoch(record.get('timestamp'))
    raise ValueError('No usage snapshot')


def _gemini_bars(data, now, prefer=None):
    """``retrieveUserQuota`` buckets: one per model, ``remainingFraction`` 0–1."""
    best={}
    for b in data.get('buckets') or []:
        if not isinstance(b,dict): continue
        frac=b.get('remainingFraction')
        if type(frac) not in (int,float) or not math.isfinite(frac) or not 0<=frac<=1: continue
        model=str(b.get('modelId') or b.get('tokenType') or 'quota')
        label=model.removeprefix('gemini-').removesuffix('-preview')[:30]
        bar=window(label,round((1-frac)*100,1),b.get('resetTime'),now)
        if bar and (label not in best or bar['percent']>best[label]['percent']): best[label]=bar
    bars=sorted(best.values(),key=lambda b:-b['percent'])
    if prefer:
        key=prefer.lower().replace(' ','-').removeprefix('gemini-')
        bars.sort(key=lambda b:0 if key and key in b['label'].lower() else 1)
    return bars[:4]


def read_source(spec, now, prefer=None):
    path=Path(spec['path']).expanduser()
    kind=spec['format']
    raw=_read(path,tail=kind=='codex-rollout')
    stamp=path.stat().st_mtime
    if kind=='codex-rollout':
        data,when=_codex_limits(raw); stamp=when or stamp
        pairs=[('5h','primary','used_percent','resets_at'),('7d','secondary','used_percent','resets_at')]
    elif kind=='claude-statusline':
        snap=json.loads(raw)
        data=snap.get('rate_limits') or {}
        stamp=epoch(snap.get('captured_at')) or stamp
        pairs=[('5h','five_hour','used_percentage','resets_at'),('7d','seven_day','used_percentage','resets_at'),
               ('7d Opus','seven_day_opus','used_percentage','resets_at'),
               ('7d Sonnet','seven_day_sonnet','used_percentage','resets_at')]
    elif kind=='chatgpt-snapshot':
        data=json.loads(raw).get('rate_limit') or {}
        pairs=[('5h','primary_window','used_percent','reset_at'),('7d','secondary_window','used_percent','reset_at')]
    elif kind=='gemini-quota':
        data=json.loads(raw)
        stamp=epoch(data.get('captured_at')) or stamp
        pairs=None
    else: raise ValueError('Unknown usage source')
    if pairs is None:
        bars=_gemini_bars(data,now,prefer)
    else:
        bars=[]
        for label,key,pct,reset in pairs:
            w=data.get(key) or {}
            if not isinstance(w,dict): continue
            when=w.get(reset)
            if when is None and type(w.get('resets_in_seconds')) in (int,float):
                when=stamp+w['resets_in_seconds']   # older Codex rollouts
            b=window(_label(w,label),w.get(pct),when,now)
            if b: bars.append(b)
            if len(bars)==4: break
    if not bars: return {'text':'No current reading','tone':'muted','title':'No unexpired usage window'}
    stale=now-stamp>STALE_SECONDS or stamp>now+30
    worst=max(b['percent'] for b in bars)
    tone='muted' if stale else 'error' if worst>=90 else 'warning' if worst>=70 else 'ok'
    return {'text':'Cached' if stale else '', 'tone':tone, 'bars':bars,
            'title':f"{spec.get('account','Explicit source')} · {kind} · "
                    f"{datetime.fromtimestamp(stamp).astimezone().isoformat(timespec='seconds')}"}


def collect(context,settings):
    sources=settings.get('sources',{})
    if not isinstance(sources,dict) or len(sources)>200: raise ValueError('Invalid usage mapping')
    now=time.time()
    agents={a.get('name'):a for a in context.get('agents') or [] if isinstance(a,dict)}
    resolved=dict(sources)
    if settings.get('auto'):
        for name,source in usage_sources.discover(context.get('agents') or [],settings).items():
            resolved.setdefault(name,source)   # explicit mapping always wins
    patches=[]
    for identity,source in list(resolved.items())[:200]:
        prefer=(agents.get(identity) or {}).get('label')
        try: value=read_source(source,now,prefer)
        except Exception: value={'text':'Unavailable','tone':'muted','title':'Usage source unavailable; account not inferred'}
        patches.append({'row_id':identity,'cells':{'limits':value}})
    return {'columns':[{'id':'limits','title':'Usage'}],'patches':patches}
