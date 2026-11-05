import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from rsi.skills import access
from rsi.skills.directory import read_skill_directory, directory_schema
from rsi.skills.store import SkillStore
from rsi.skills.budget import render_context, select_context, BudgetError
from rsi.core.common import read_json, write_json
from rsi.agent.runner import run_experiment
from rsi.experiment import checkpoints
from test_reference_consolidation import sample
from test_skill_routing import CharacterCounter
from test_core_baseline import Backend, Environment, Models
import test_maintenance_transactions as p0


def access_config():
    return {'skill_directory':{'schema':1},'skill_routing':{'enabled':True},'skill_access':{'enabled':True}}


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.files=sample()
        self.skill=SkillStore(self.root/'store',self.files,directory_schema=1).current()
        self.snapshot=access.Snapshot(self.skill);self.addCleanup(self.snapshot.cleanup)
        self.env=p0.LocalEnvironment(self.root/'installed')
        self.options=access.settings(access_config())

    def test_complete_copy_manifest_and_explicit_degradation(self):
        receipt=access.install(self.env,self.snapshot,self.options,self.root)
        self.assertFalse(receipt['read_only_enforced']);self.assertTrue(receipt['degraded'])
        self.assertEqual(receipt['status'],'verified')
        self.assertEqual(read_skill_directory(self.root/'installed',directory_schema=1),self.files)
        self.assertEqual(receipt['snapshot_sha256'],receipt['installed_snapshot_sha256'])
        self.assertEqual(read_skill_directory(self.skill,directory_schema=1),self.files)

    def test_requires_strong_mode_never_copies(self):
        for options in (self.options|{'require_read_only':True},self.options|{'install_mode':'read_only'}):
            with self.assertRaisesRegex(RuntimeError,'downgrade'):access.install(self.env,self.snapshot,options,self.root)
            self.assertFalse((self.root/'installed').exists())
            self.assertEqual(read_json(self.root/'skill-install.json')['status'],'failed')

    def test_tampered_installed_snapshot_detected(self):
        access.install(self.env,self.snapshot,self.options,self.root)
        target=self.root/'installed/references/a.md'
        target.write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'integrity'):access.verify(self.env,self.snapshot.expected)

    def test_extra_installed_file_detected(self):
        access.install(self.env,self.snapshot,self.options,self.root)
        (self.root/'installed/extra.md').write_text('extra')
        with self.assertRaises(RuntimeError):access.verify(self.env,self.snapshot.expected)

    def test_missing_installed_reference_detected(self):
        access.install(self.env,self.snapshot,self.options,self.root)
        (self.root/'installed/references/a.md').unlink()
        with self.assertRaises(RuntimeError):access.verify(self.env,self.snapshot.expected)

    def test_wrong_version_and_modified_fixed_tool_rejected(self):
        wrong=self.root/'0000000000000000';shutil.copytree(self.skill,wrong)
        with self.assertRaises(ValueError):access.Snapshot(wrong)
        (self.skill/'scripts/inspect_project.py').write_text('changed')
        with self.assertRaises(ValueError):access.Snapshot(self.skill)

    def test_local_missing_reference_and_symlink_rejected(self):
        (self.skill/'references/a.md').unlink()
        with self.assertRaises(ValueError):access.Snapshot(self.skill)
        with patch.object(Path,'is_symlink',return_value=True),self.assertRaises(ValueError):access.Snapshot(self.skill)

    def test_snapshot_only_contains_validated_files(self):
        self.assertEqual(read_skill_directory(self.snapshot.path,directory_schema=1),self.files)
        self.assertNotEqual(self.snapshot.path,self.skill)
        self.assertEqual(set(self.snapshot.expected),set(self.files))
        self.assertNotIn('checkpoint.json',self.snapshot.expected)

    def test_strict_configuration(self):
        for block in ({'enabled':1},{'enabled':True,'install_mode':'chmod'}, {'bogus':True},
                      {'enabled':True,'verify_snapshot_after':False}, {'enabled':True,'verify_snapshot_before':False},
                      {'enabled':True,'require_read_only':True,'install_mode':'copy_audited'}):
            with self.subTest(block=block),self.assertRaises(ValueError):access.settings(access_config()|{'skill_access':block})
        for config in ({'skill_access':{'enabled':True}},access_config()|{'skill_update_method':'reasoningbank'},
                       access_config()|{'skill_routing':{'enabled':False}}):
            with self.assertRaises(ValueError):directory_schema(config)

    def test_disabled_context_is_identical_and_enabled_note_budgeted(self):
        original=render_context(self.files)
        self.assertEqual(original,render_context(self.files,access=access.settings({})))
        text=render_context(self.files,access=self.options)
        self.assertIn('Do not modify /opt/rsi-skills/',text)
        self.assertGreater(len(text),len(original))
        with self.assertRaises(BudgetError):select_context(self.files,[],CharacterCounter('solver'),budget_limit=len(original),top_k=2,access=self.options)

    def test_p4_estimated_proxy_counts_complete_guidance(self):
        from rsi import ROOT
        from rsi.core.config import load_config
        from rsi.skills.routing import route
        config=load_config(ROOT/'configs/agent/routing-estimated-gpt6-luna.yaml')
        config['skill_access']={'enabled':True}
        routed=route({'id':'mock','prompt':'pytest'},self.skill,config)
        record=routed.record()
        self.assertEqual(record['counter']['accuracy'],'estimated')
        self.assertLessEqual(routed.context.initial_skill_tokens,record['estimated_budget']['effective_limit'])
        self.assertFalse(record['estimated_budget']['strict_guarantee'])
        self.assertIn('Autonomous Skill access',routed.context.text)

    def test_index_exposure_option_controls_instruction_not_permissions(self):
        text=render_context(self.files,access=self.options|{'expose_index':False})
        self.assertNotIn('routing/index.yaml',text)
        self.assertIn('routing/index.yaml',self.files)

    def test_docker_args_only_private_snapshot(self):
        args=access.docker_args(self.snapshot.path)
        self.assertIn('readonly',args[1]);self.assertIn(str(self.snapshot.path.resolve()),args[1])
        self.assertNotIn(str(self.skill.resolve()),args[1]);self.assertIn('--cap-drop=ALL',args)
        with self.assertRaises(ValueError):access.docker_args(self.root/'comma,path')

    def test_read_only_inspection_and_all_five_denial_probes(self):
        env=SimpleNamespace(config=SimpleNamespace(executable='docker'),container_id='mock')
        info=[{'Mounts':[{'Destination':'/opt/rsi-skills','RW':False,'Type':'bind'}],
               'HostConfig':{'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true']}}]
        result=SimpleNamespace(stdout=json.dumps(info))
        probes={k:True for k in ('create','overwrite','modify','delete','rename')}
        with patch.object(access.subprocess,'run',return_value=result),patch.object(access,'remote_json',return_value=probes):
            self.assertEqual(access.verify_read_only(env),probes)
        info[0]['Mounts'][0]['RW']=True
        with patch.object(access.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(info))),self.assertRaises(RuntimeError):access.verify_read_only(env)

    def test_permission_only_probe_does_not_claim_read_only(self):
        info=[{'Mounts':[{'Destination':'/opt/rsi-skills','RW':False,'Type':'bind'}],
               'HostConfig':{'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges:true']}}]
        env=SimpleNamespace(config=SimpleNamespace(executable='docker'),container_id='mock')
        with patch.object(access.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(info))),patch.object(access,'remote_json',return_value={'create':True}),self.assertRaises(RuntimeError):access.verify_read_only(env)


class AccessIntegrationTests(unittest.TestCase):
    def setUp(self):
        p0.ManagedCheckpointTests.setUp(self)
        self.config.update(skill_routing={'enabled':True,'max_initial_skill_tokens':20000},skill_access={'enabled':True})
        files=sample({'references/a.md':'# pytest\npytest fixture scope.\n','references/b.md':'# pytest two\npytest fixture tests.\n',
                      'references/third.md':'# Other\nUNLOADED_REFERENCE_BODY\n'})
        self.seed=SkillStore(Path(self.tmp.name)/'seed',files,directory_schema=1).current()
        self.config['initial_skill']=str(self.seed);self.tasks[0]['prompt']='pytest fixture'
        self.models=Models(no_changes=True)
        original=Environment.execute
        def execute(env,command,cwd=''):
            if 'RSI_ACCESS_MANIFEST=' in command:
                return {'returncode':0,'output':'RSI_ACCESS_MANIFEST='+json.dumps(access.manifest(env.skill))}
            if command=='cat /opt/rsi-skills/references/third.md':
                return {'returncode':0,'output':env.skill['references/third.md']}
            if command=='tamper':
                env.skill['references/third.md']='bad'
                return {'returncode':0,'output':''}
            return original(env,command,cwd)
        mock=patch.object(Environment,'execute',execute);mock.start();self.addCleanup(mock.stop)
        self.action='cat /opt/rsi-skills/references/third.md'
        def factory(options):
            client=self.models(options)
            if options['name']=='solver':
                query=client.query
                def ask(messages,**kwargs):
                    reply=query(messages,**kwargs)
                    if client.n_calls==1:reply={'content':'```bash\n'+self.action+'\n```'}
                    return reply
                client.query=ask
            return client
        self.factory=factory

    def run_tasks(self,resume=False):
        return run_experiment(self.tasks,self.config,self.output,resume=resume,backend_factory=Backend,
                              model_factory=self.factory,counter_factory=lambda cfg:CharacterCounter(cfg['name']))

    def test_five_shared_contexts_independent_reads_and_nonadditive_usage(self):
        reports=self.run_tasks();systems=[]
        for i in range(5):
            root=self.output/f'task/rollout-{i}'
            data=read_json(root/'trajectory.json');summary=data['skill_access']
            systems.append(data['messages'][0]['content'])
            self.assertNotIn('UNLOADED_REFERENCE_BODY',systems[-1])
            self.assertEqual(summary['unique_references_read'],['references/third.md'])
            self.assertEqual(summary['autonomous_read_events'],1)
            self.assertEqual(summary['solver_input_tokens_api'],20)
            self.assertFalse(summary['cost_addition_allowed'])
            events=[json.loads(line) for line in (root/'skill-access.jsonl').read_text().splitlines()]
            self.assertEqual(events[1]['classification'],'content_confirmed')
            self.assertEqual(events[1]['rollout'],i)
            self.assertEqual(data['artifact'],f'patch-rollout-{i}')
            self.assertEqual(len(summary['automatically_injected_references']),2)
            self.assertFalse(data['skill_install']['read_only_enforced'])
        self.assertEqual(len(set(systems)),1)
        self.assertEqual(reports[0]['input_tokens'],10*len(self.models.requests))
        before=len(self.models.requests);self.run_tasks(resume=True)
        self.assertEqual(len(self.models.requests),before)

    def test_tampering_aborts_before_grading_and_checkpoint(self):
        self.action='tamper'
        with self.assertRaises(RuntimeError):self.run_tasks()
        self.assertEqual(read_json(self.output/'checkpoint.json')['completed'],[])
        for i in range(5):
            data=read_json(self.output/f'task/rollout-{i}/trajectory.json')
            self.assertIn('integrity',data['error']);self.assertEqual(data['artifact'],'')
        self.assertEqual(read_skill_directory(self.seed,directory_schema=1)['references/third.md'],'# Other\nUNLOADED_REFERENCE_BODY\n')

    def test_audit_failure_is_unknown_not_solver_failure(self):
        with patch('rsi.skills.access_audit.audit',side_effect=OSError('audit log unavailable')):
            self.run_tasks()
        data=read_json(self.output/'task/rollout-0/trajectory.json')
        self.assertIsNone(data['error']);self.assertEqual(data['skill_access']['status'],'unknown')
        self.assertIsNone(data['skill_access']['autonomous_read_events'])
        self.run_tasks(resume=True)

    def test_required_read_only_failure_does_not_call_model(self):
        self.config['skill_access']['require_read_only']=True
        with self.assertRaises(RuntimeError):self.run_tasks()
        self.assertEqual(self.models.requests,[])

    def test_p3_optional_failure_and_p4_receipts_resume_together(self):
        self.config.update(skill_maintenance={'enabled':True},
                           reference_maintenance={'enabled':True,'interval_accepted_updates':1})
        self.models.no_changes=False
        self.run_tasks()
        state=read_json(self.output/'checkpoint.json')
        update=state['completed'][0]['update']
        self.assertTrue(update['accepted'])
        self.assertEqual(update['consolidation']['status'],'consolidation_failed')
        self.assertEqual(update['learning_skill_version'],update['after_skill_version'])
        self.assertEqual(state['reference_maintenance']['accepted_updates'],1)
        before=len(self.models.requests);self.run_tasks(resume=True)
        self.assertEqual(len(self.models.requests),before)

    def test_corrupted_receipt_and_changed_resume_config_rejected(self):
        self.run_tasks()
        saved=read_json(self.output/'task/rollout-0/skill-install.json')
        write_json(self.output/'task/rollout-0/skill-install.json',saved|{'final_status':'tampered'})
        with self.assertRaisesRegex(ValueError,'receipt hash'):checkpoints.resume_request(self.output)
        write_json(self.output/'task/rollout-0/skill-install.json',saved)
        self.config['skill_access']['audit_reads']=False
        with self.assertRaises(ValueError):self.run_tasks(resume=True)


class DockerAccessIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').getenv('RSI_TEST_DOCKER')=='1','opt-in real Docker test; use RSI_TEST_DOCKER=1')
    def test_real_read_only_mount_five_mutations_and_patch_boundary(self):
        import os
        from rsi.benchmarks.backends import SWEBackend
        from test_core_baseline import test_config
        with tempfile.TemporaryDirectory(prefix='rsi-p4-docker-') as tmp:
            root=Path(tmp);files=sample()
            skill=SkillStore(root/'store',files,directory_schema=1).current()
            snapshot=access.Snapshot(skill)
            image=os.environ['RSI_TEST_DOCKER_IMAGE']
            config=test_config()|access_config()|{'_skill_access_snapshot':str(snapshot.path)}
            task={'id':'p4-local-integration','instance':{'instance_id':'p4-local-integration','image_name':image}}
            backend=None
            try:
                backend=SWEBackend(task,root,config)
                receipt=access.install(backend.env,snapshot,access.settings(config),root)
                self.assertTrue(receipt['read_only_enforced']);self.assertTrue(all(receipt['write_probes'].values()))
                baseline=backend.env.execute('git status --porcelain')['output']
                self.assertEqual(baseline.strip(),'')
                response=backend.env.execute('cat /opt/rsi-skills/references/a.md')
                self.assertEqual(response['returncode'],0);self.assertEqual(response['output'],files['references/a.md'])
                self.assertEqual(backend.env.execute('git status --porcelain')['output'],baseline)
                self.assertEqual(backend.env.execute("printf 'P4 repository change\\n' > .p4-test-evidence.txt")['returncode'],0)
                artifact=backend.artifact()
                self.assertIn('diff --git a/.p4-test-evidence.txt',artifact)
                self.assertNotIn('/opt/rsi-skills',artifact)
                self.assertEqual(artifact.count('diff --git '),1)
                receipt['final_snapshot_sha256']=access.verify(backend.env,snapshot.expected)
                receipt['final_status']='verified'
                self.assertEqual(read_skill_directory(skill,directory_schema=1),files)
                evidence=os.getenv('RSI_TEST_ARTIFACT_DIR')
                if evidence:
                    target=Path(evidence);target.mkdir(parents=True,exist_ok=True)
                    write_json(target/'docker-validation.json',{'image':image,'installation':receipt,
                        'read_response':response,'git_status_before_after_reads':baseline,
                        'patch':artifact,'five_write_operations_denied':True})
            finally:
                if backend:backend.close()
                snapshot.cleanup()

    @unittest.skipUnless(__import__('os').getenv('RSI_TEST_DOCKER')=='1','opt-in real Harbor Docker lifecycle')
    def test_real_harbor_upload_and_tamper_detection(self):
        from rsi.benchmarks.backends import TerminalBackend
        from test_core_baseline import test_config
        with tempfile.TemporaryDirectory(prefix='rsi-p4-harbor-') as tmp:
            root=Path(tmp);task=root/'task'
            (task/'environment').mkdir(parents=True);(task/'tests').mkdir()
            (task/'task.toml').write_text('schema_version = "1.4"\n[environment]\ndocker_image = "python:3.12-slim"\n[verifier]\ntimeout_sec = 30\n',encoding='utf-8')
            (task/'instruction.md').write_text('Local P4 installation test. No model.',encoding='utf-8')
            (task/'environment/Dockerfile').write_text('FROM python:3.12-slim\n',encoding='utf-8')
            (task/'tests/test.sh').write_text('#!/bin/bash\nexit 0\n',encoding='utf-8')
            skill=SkillStore(root/'store',sample(),directory_schema=1).current()
            snapshot=access.Snapshot(skill);backend=None
            try:
                config=test_config()|access_config()
                backend=TerminalBackend({'path':str(task)},root/'trial',config)
                receipt=access.install(backend.env,snapshot,access.settings(config),root)
                self.assertFalse(receipt['read_only_enforced']);self.assertTrue(receipt['degraded'])
                self.assertEqual(backend.env.execute('cat /opt/rsi-skills/references/a.md')['output'],snapshot.files['references/a.md'])
                self.assertEqual(access.verify(backend.env,snapshot.expected),receipt['snapshot_sha256'])
                self.assertEqual(backend.env.execute("printf 'changed' > /opt/rsi-skills/references/a.md")['returncode'],0)
                with self.assertRaises(RuntimeError):access.verify(backend.env,snapshot.expected)
                self.assertEqual(read_skill_directory(skill,directory_schema=1),snapshot.files)
            finally:
                if backend:backend.close()
                snapshot.cleanup()
