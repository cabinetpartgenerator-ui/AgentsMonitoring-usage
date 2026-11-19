import json
from pathlib import Path
import tempfile
import time
import unittest
from agentsmon import usage_plugin as usage


class AccountUsage(unittest.TestCase):
    def test_accounts_never_share_by_vendor_missing_source_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'status.json'
            p.write_text(json.dumps({'rate_limits':{'five_hour':{'used_percentage':20,'resets_at':time.time()+3000}}}))
            result=usage.collect({}, {'sources':{'Alice':{'path':str(p),'format':'claude-statusline','account':'A'},
                                                'Bob':{'path':str(p)+'-missing','format':'claude-statusline','account':'B'}}})
            self.assertEqual(result['patches'][0]['cells']['limits']['bars'][0]['percent'],20)
            self.assertEqual(result['patches'][1]['cells']['limits']['text'],'Unavailable')

    def test_expired_negative_nan_and_percentage_above_100_do_not_fake_a_limit(self):
        for pct in (-1,101,float('nan'),True,'90'):
            self.assertIsNone(usage.window('5h',pct,300,100))
        self.assertIsNone(usage.window('5h',30,99,100))
        self.assertEqual(usage.window('5h',30,'2030-01-01T00:00:00Z',100)['percent'],30)

    def test_rollout_tail_and_real_window_labels(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'rollout.jsonl'
            p.write_text(json.dumps({'timestamp':'2026-10-15T12:00:00Z','payload':{'rate_limits':{
                'primary':{'used_percent':42,'window_minutes':180,'resets_at':2_000_000_000}}}})+'\n')
            result=usage.read_source({'path':str(p),'format':'codex-rollout'},1_800_000_000)
            self.assertEqual(result['bars'][0]['label'],'3h')


class ResetAndRemaining(unittest.TestCase):
    def test_bar_reports_remaining_and_reset_epoch(self):
        bar=usage.window('5h',23,2_000_000_000,100)
        self.assertEqual((bar['remaining'],bar['reset']),(77,2_000_000_000))
        self.assertNotIn('reset',usage.window('5h',23,None,100))
        self.assertEqual(usage.window('5h',23,2_000_000_000_000,100)['reset'],2_000_000_000)  # milliseconds

    def test_older_codex_rollout_reset_in_seconds_is_relative_to_record(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'rollout.jsonl'
            p.write_text(json.dumps({'timestamp':'2026-10-15T12:00:00Z','payload':{'rate_limits':{
                'primary':{'used_percent':10,'window_minutes':300,'resets_in_seconds':600}}}})+'\n')
            start=usage.epoch('2026-10-15T12:00:00Z')
            bar=usage.read_source({'path':str(p),'format':'codex-rollout'},start+60)['bars'][0]
            self.assertEqual(bar['reset'],start+600)

    def test_reset_survives_plugin_validation(self):
        from agentsmon import plugins
        out=plugins.cell({'text':'','bars':[{'label':'5h','percent':20,'remaining':80,'reset':2e9}]})
        self.assertEqual(out['bars'][0]['reset'],2_000_000_000)
        with self.assertRaises(ValueError): plugins.cell({'bars':[{'label':'5h','percent':20,'reset':-5}]})


class GeminiQuota(unittest.TestCase):
    def test_buckets_become_used_bars_with_preferred_model_first(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'gemini.json'
            p.write_text(json.dumps({'captured_at':1000,'buckets':[
                {'modelId':'gemini-2.5-flash','remainingFraction':0.9,'resetTime':'2030-01-01T00:00:00Z'},
                {'modelId':'gemini-2.5-pro','remainingFraction':0.25,'resetTime':'2030-01-01T00:00:00Z'},
                {'modelId':'bad','remainingFraction':2}]}))
            result=usage.read_source({'path':str(p),'format':'gemini-quota'},1100,prefer='Gemini 2.5 Flash')
            self.assertEqual([b['label'] for b in result['bars']],['2.5-flash','2.5-pro'])
            self.assertEqual(result['bars'][1]['percent'],75)
            self.assertEqual(result['tone'],'warning')

    def test_refresh_uses_cached_token_and_writes_snapshot(self):
        from agentsmon import usage_sources as src
        import io, os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            creds=Path(d)/'oauth_creds.json'
            calls=[]
            def post(method,token,body):
                calls.append((method,token,body))
                return {'cloudaicompanionProject':'proj-1'} if method=='loadCodeAssist' else \
                       {'buckets':[{'modelId':'gemini-2.5-pro','remainingFraction':0.5,'resetTime':'2030-01-01T00:00:00Z'}]}
            with patch.dict(os.environ,{'XDG_STATE_HOME':d}):
                creds.write_text(json.dumps({'access_token':'tok','expiry_date':(time.time()-5)*1000}))
                self.assertEqual(src.gemini_refresh(creds,io.StringIO(),post=post),2)   # expired: no request
                self.assertEqual(calls,[])
                creds.write_text(json.dumps({'access_token':'tok','expiry_date':(time.time()+3600)*1000}))
                self.assertEqual(src.gemini_refresh(creds,io.StringIO(),post=post),0)
                self.assertEqual(calls[1],('retrieveUserQuota','tok',{'project':'proj-1'}))
                snap=json.loads((Path(d)/'agentsmon/usage/gemini.json').read_text())
                self.assertNotIn('tok',json.dumps(snap))
                self.assertEqual(oct((Path(d)/'agentsmon/usage/gemini.json').stat().st_mode&0o777),'0o600')


class AutoDiscovery(unittest.TestCase):
    def test_each_agent_gets_only_its_own_session_snapshot(self):
        from agentsmon import usage_sources as src
        import io, os
        from unittest.mock import patch
        sid_codex='019a0000-1111-2222-3333-444455556666'; sid_claude='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'XDG_STATE_HOME':d,'CODEX_HOME':d+'/codex'}):
            day=Path(d)/'codex/sessions/2026/11/18'; day.mkdir(parents=True)
            (day/f'rollout-2026-11-18T10-00-00-{sid_codex}.jsonl').write_text(json.dumps({'timestamp':'2026-11-18T10:00:00Z',
                'payload':{'rate_limits':{'primary':{'used_percent':40,'window_minutes':300,'resets_at':time.time()+900}}}})+'\n')
            status={'session_id':sid_claude,'model':{'display_name':'Opus'},
                    'rate_limits':{'five_hour':{'used_percentage':12,'resets_at':time.time()+3600}}}
            out=io.StringIO()
            src.claude_statusline(io.StringIO(json.dumps(status)),out)
            self.assertIn('5h 88% left',out.getvalue())
            agents=[{'name':'cx','kind':'codex','session_id':sid_codex},
                    {'name':'cl','kind':'claude-code','session_id':sid_claude},
                    {'name':'cl2','kind':'claude-code','session_id':'ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee'},
                    {'name':'evil','kind':'claude-code','session_id':'../../etc/passwd'},
                    {'name':'gm','kind':'gemini-cli','session_id':None}]
            result=usage.collect({'agents':agents},{'auto':True,'sources':{}})
            cells={p['row_id']:p['cells']['limits'] for p in result['patches']}
            self.assertEqual(set(cells),{'cx','cl'})     # no snapshot → no guess from another session
            self.assertEqual(cells['cx']['bars'][0]['remaining'],60)
            self.assertEqual(cells['cl']['bars'][0]['percent'],12)
            # Explicit mapping still wins over discovery.
            result=usage.collect({'agents':agents},{'auto':True,'sources':{'cl':{'path':d+'/missing','format':'claude-statusline'}}})
            self.assertEqual({p['row_id']:p['cells']['limits'].get('text') for p in result['patches']}['cl'],'Unavailable')
