import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rsi.skills.access_audit import audit, detect, sha_bytes
from rsi.skills.budget import SkillContext, CounterInfo
from rsi.core.common import write_json,read_json
from test_skill_access import access_config
from test_skill_routing import CharacterCounter


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.folder=self.root/'rollout-0';(self.folder/'commands').mkdir(parents=True)
        self.files={'references/a.md':'Alpha evidence.\n','references/b.md':'Beta.\n'}
        self.context=SkillContext('context','hash',7,('references/b.md',),'version','index','route')
        self.config=access_config()|{'model':{'name':'solver'}}
        write_json(self.root/'skill-route.json',{'counter':{'accuracy':'test_only','identity':'fixture'}})
        self.result={'rollout':0,'usage':{'input_tokens':100,'output_tokens':25},'messages':[]}

    def command(self,command='cat /opt/rsi-skills/references/a.md',output=None,code=0,cwd=''):
        i=len(self.result['messages'])//2+1
        output=self.files['references/a.md'] if output is None else output
        observation='<returncode>'+str(code)+'</returncode>\n'+output
        self.result['messages'] += [{'role':'assistant','content':command},{'role':'user','content':observation}]
        raw=output.encode();name=f'{i:06d}.output.txt';(self.folder/'commands'/name).write_bytes(raw)
        write_json(self.folder/'commands'/f'{i:06d}.json',{'sequence':i,'assistant_message':2*(i-1),
            'command':command,'cwd':cwd,'returncode':code,'error':None,'output':{'path':name,'sha256':sha_bytes(raw),'bytes':len(raw)}})
        return observation

    def run_audit(self):
        return audit(self.folder,self.files,self.context,self.config,self.result,counter_factory=lambda cfg:CharacterCounter(cfg['name']))

    def events(self):return [json.loads(v) for v in (self.folder/'skill-access.jsonl').read_text().splitlines()]

    def test_simple_cat_corroborates_full_content_and_keeps_injection_separate(self):
        obs=self.command();summary=self.run_audit();events=self.events()
        self.assertEqual(events[0]['event_type'],'automatic_injection')
        self.assertEqual(events[1]['classification'],'content_confirmed')
        self.assertEqual(summary['unique_references_read'],['references/a.md'])
        self.assertEqual(summary['automatically_injected_references'],['references/b.md'])
        self.assertEqual(summary['autonomous_observation_tokens_estimated'],len(obs))
        self.assertEqual(summary['solver_input_tokens_api'],100)
        self.assertEqual(events[1]['observation_sha256'],sha_bytes(obs.encode()))

    def test_repeated_reads_count_events_but_deduplicate_files(self):
        self.command();self.command();summary=self.run_audit()
        self.assertEqual(summary['autonomous_read_events'],2)
        self.assertEqual(summary['unique_references_read'],['references/a.md'])
        self.assertEqual([e['command_index'] for e in self.events()[1:]],[1,2])

    def test_multiple_paths_one_observation_not_double_counted(self):
        obs=self.command('cat /opt/rsi-skills/references/a.md /opt/rsi-skills/references/b.md','Alpha and beta')
        summary=self.run_audit()
        self.assertEqual(summary['autonomous_observation_tokens_estimated'],len(obs))
        self.assertEqual(summary['autonomous_read_events'],1)
        self.assertEqual(len(self.events()[1]['paths']),2)
        self.assertEqual(self.events()[1]['classification'],'inferred')

    def test_echo_path_is_not_a_confirmed_read(self):
        self.command('echo /opt/rsi-skills/references/a.md','/opt/rsi-skills/references/a.md')
        summary=self.run_audit()
        self.assertIsNone(summary['autonomous_read_events'])
        self.assertIsNone(self.events()[1]['read_detected'])

    def test_failed_command_is_unknown_even_if_partial_output_matches(self):
        self.command(code=1);summary=self.run_audit()
        self.assertEqual(self.events()[1]['classification'],'failed_or_unknown')
        self.assertIsNone(summary['unique_references_read'])
        self.assertIsNone(summary['autonomous_read_events'])

    def test_content_mismatch_is_not_corroborated(self):
        self.command(output='other output');self.run_audit()
        self.assertEqual(self.events()[1]['classification'],'inferred')
        self.assertIsNone(self.events()[1]['read_detected'])

    def test_reader_commands_python_relative_and_combined(self):
        for command in ['sed -n 1,2p /opt/rsi-skills/references/a.md','head /opt/rsi-skills/references/a.md',
                        'tail /opt/rsi-skills/references/a.md','grep Alpha /opt/rsi-skills/references/a.md',
                        'rg Alpha /opt/rsi-skills/references/a.md',"awk '{print}' /opt/rsi-skills/references/a.md",
                        "python -c \"print(open('/opt/rsi-skills/references/a.md').read())\"",
                        'cd /opt/rsi-skills && cat references/a.md',
                        'cat /opt/rsi-skills/references/a.md > /tmp/output',
                        'false && cat /opt/rsi-skills/references/a.md',
                        'wc -l < /opt/rsi-skills/references/a.md']:
            with self.subTest(command=command):
                paths,simple,reader=detect(command,'/testbed')
                self.assertIn('references/a.md',paths);self.assertTrue(reader);self.assertFalse(simple)
        paths,_,reader=detect("python -c \"open('references/a.md').read()\"",'/opt/rsi-skills')
        self.assertIn('references/a.md',paths);self.assertTrue(reader)

    def test_relative_path_simple_cat_and_traversal(self):
        self.command('cat a.md',cwd='/opt/rsi-skills/references');self.run_audit()
        self.assertEqual(self.events()[1]['classification'],'content_confirmed')
        self.assertEqual(detect('cat /opt/rsi-skills/../../etc/passwd','')[0],[])

    def test_dynamic_path_unknown_never_recorded_as_zero_reads(self):
        self.command('eval "$SCRIPT"','');summary=self.run_audit()
        self.assertIsNone(summary['autonomous_read_events']);self.assertFalse(summary['absence_of_events_proves_no_reads'])

    def test_no_commands_is_unknown_not_proof_of_absence(self):
        summary=self.run_audit();self.assertIsNone(summary['autonomous_read_events'])
        self.assertIsNone(summary['autonomous_observation_tokens_estimated'])

    def test_estimated_counter_identity_preserved(self):
        self.command()
        class Counter:
            info=CounterInfo('solver','proxy-test','estimated','Mock proxy, not provider tokens')
            def count(self,text):return len(text)
        with patch('rsi.skills.access_audit.routing_settings',return_value={'budget_mode':'estimated'}):
            summary=audit(self.folder,self.files,self.context,self.config,self.result,counter_factory=lambda cfg:Counter())
        self.assertEqual(summary['observation_counter']['accuracy'],'estimated')
        self.assertFalse(summary['cost_addition_allowed'])

    def test_unknown_counter_does_not_invent_tokens(self):
        self.command()
        summary=audit(self.folder,self.files,self.context,self.config,self.result)
        self.assertIsNone(summary['autonomous_observation_tokens_estimated'])
        self.assertIn('counter_error',summary)

    def test_audit_disabled_and_cost_disabled(self):
        self.command();self.config['skill_access']['record_observation_cost']=False
        self.assertIsNone(self.run_audit()['autonomous_observation_tokens_estimated'])
        self.config['skill_access']['audit_reads']=False
        summary=self.run_audit();self.assertEqual(summary['status'],'disabled');self.assertIsNone(summary['autonomous_read_events'])

    def test_tampered_output_and_path_traversal_rejected(self):
        self.command();path=self.folder/'commands/000001.output.txt';path.write_text('altered')
        with self.assertRaises(ValueError):self.run_audit()
        record=read_json(self.folder/'commands/000001.json');record['output']['path']='../escape'
        write_json(self.folder/'commands/000001.json',record)
        with self.assertRaises(ValueError):self.run_audit()
