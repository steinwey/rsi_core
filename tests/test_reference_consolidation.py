import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml
from rsi.skills import consolidation as c
from rsi.skills.consolidation_state import initial_state, schedule
from rsi.skills.store import load_initial_skill, SkillStore
from rsi.skills.index import build_index, validate_index
from rsi.skills.directory import INDEX_PATH, directory_schema, read_skill_directory
from rsi.skills.markdown_units import sha, rebase_links
from rsi.skills.migration import split_references
from rsi.core.common import digest, read_json, write_json
from rsi.agent.runner import run_experiment
from rsi.experiment import checkpoints
from rsi.skills.routing import retrieve_bm25
from test_skill_migration import PlannerModels, section
from test_core_baseline import Backend, Models
import test_maintenance_transactions as p0


def config():
    return {'skill_directory': {'schema': 1}, 'skill_update_method': 'trace2skill',
            'skill_maintenance': {'enabled': True}, 'reference_maintenance': {'enabled': True},
            'skill_editor_model': {'name': 'mock', 'max_tokens': 8192}}


def sample(refs=None, metadata=None):
    files, _ = load_initial_skill(directory_schema=1, bootstrap=True)
    files.update(refs or {'references/a.md': '# A\nWhen pytest fails, inspect evidence.\nCheck scope.\n',
                          'references/b.md': '# B\nWhen pytest fails, inspect evidence.\nVerify callers.\n'})
    files[INDEX_PATH] = build_index(files)
    index = validate_index(files)
    for entry in index['entries']:
        entry.update((metadata or {}).get(entry['path'], {}))
    files[INDEX_PATH] = yaml.safe_dump(index, sort_keys=True)
    return files


def plan_for(files, operation='MERGE', paths=None, target='references/merged.md'):
    paths = sorted(paths or [p for p in files if p.startswith('references/')])
    value = {'schema': 1, 'input_hash': digest(files), 'operation': operation,
             'sources': [c.describe(files, p, validate_index(files)) for p in paths],
             'targets': {}, 'coverage': [], 'reference_changes': [], 'reason': 'Retain all original knowledge and conditions.'}
    if operation in ('KEEP_SEPARATE', 'CONFLICT_UNRESOLVED'):
        return value
    if operation == 'SPLIT':
        result, records = split_references(files, 5000, sources=paths)
        value['targets'] = {p: result[p] for p in result if result[p] != files.get(p)}
        value['coverage'] = [{'source': r['source'], 'target': r['target'], 'unit_id': r['unit_id']} for r in records]
    else:
        value['targets'] = {target: files[target] if operation == 'DEDUPLICATE' else '\n'.join(rebase_links(files[p], p, target, files) for p in paths)}
        value['coverage'] = [{'source': p, 'target': target} for p in paths]
        value['reference_changes'] = [{'old': p, 'new': target} for p in paths if p != target]
    return value


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.files = sample(); self.config = config()

    def run_stage(self, plan=None, reviewer=None, name='stage'):
        models = PlannerModels(planner=lambda data: plan if plan is not None else plan_for(self.files), reviewer=reviewer)
        out = c.consolidate(self.files, self.config, self.root/name, model_factory=models)
        return out, read_json(self.root/name/'validation.json'), models

    def apply(self, plan=None, options=None):
        return c.apply_plan(self.files, plan or plan_for(self.files),
                            [p for p in self.files if p.startswith('references/')], options or c.settings(self.config))

    def test_schedule_exact_tenth_and_twentieth(self):
        state = initial_state(); triggered = []
        for i in range(1, 21):
            item = schedule(state, self.config, True)
            if item['due']: triggered.append(i)
            self.assertEqual(state['accepted_updates'], i-1)
            state = item['after']
        self.assertEqual(triggered, [10,20]); self.assertEqual(state, {'accepted_updates':20,'checks':2,'cursor':2})

    def test_nonupdates_never_advance_schedule(self):
        state = {'accepted_updates':9,'checks':0,'cursor':0}
        for _ in range(20):
            item = schedule(state,self.config,False)
            self.assertFalse(item['due']); self.assertEqual(item['after'],state)

    def test_config_bounds_and_requires_p1(self):
        for key,value in [('interval_accepted_updates',0),('max_candidate_files',True),('max_llm_calls',1),
                          ('timeout_seconds',-1),('max_reference_chars',5001),('max_rounds',0),('max_input_chars',1.5)]:
            with self.subTest(key=key), self.assertRaises(ValueError): c.settings({'reference_maintenance':{key:value}})
        with self.assertRaisesRegex(ValueError,'P0'): directory_schema({'reference_maintenance':{'enabled':True}})
        with self.assertRaisesRegex(ValueError,'P1'): directory_schema({'reference_maintenance':{'enabled':True},'skill_directory':{'schema':1}})
        self.assertEqual(directory_schema(self.config),1)

    def test_full_and_unit_duplicate_discovery(self):
        self.files = sample({'references/a.md':'# Same\n'+section(size=500), 'references/b.md':'# Same\n'+section(size=500)})
        result = c.discover(self.files,c.settings(self.config))
        self.assertEqual(result['pairs'][0]['kind'],'exact_file')
        self.files = sample({'references/a.md':'# Same\n'+section(size=500),
                             'references/b.md':'# Same\n'+section(size=500)+section('Click extra',size=300)})
        self.assertTrue(any(p['kind']=='exact_unit_with_context' for p in c.discover(self.files,c.settings(self.config))['pairs']))

    def test_bm25_discovery_bounded_and_rotating(self):
        self.files = sample({f'references/r{i}.md':f'# Topic {i}\npytest fixture evidence {i}\n' for i in range(20)})
        opts=c.settings(self.config)|{'max_candidate_files':4,'max_candidate_pairs':3}
        with patch.object(c,'retrieve_bm25',wraps=retrieve_bm25) as search:
            result=c.discover(self.files,opts,cursor=3)
        self.assertLessEqual(len(result['files']),4); self.assertLessEqual(len(result['pairs']),3)
        self.assertLessEqual(search.call_count,4); self.assertIn(result['anchors'][0],result['files'])
        self.assertTrue(any(p['score']>0 for p in result['pairs']))

    def test_merge_exact_coverage_and_provenance(self):
        index=validate_index(self.files)
        index['entries'][0]['provenance']=[{'task_id':'old','rollout':0,'evidence_id':'a'*64,'evidence_kind':'success'}]
        self.files[INDEX_PATH]=yaml.safe_dump(index)
        before=copy.deepcopy(self.files); out,ledger=self.apply()
        self.assertEqual(self.files,before)
        self.assertEqual(out['references/merged.md'],before['references/a.md']+'\n'+before['references/b.md'])
        self.assertEqual(len(ledger['records']),2)
        entry=validate_index(out)['entries'][0]
        self.assertEqual(len(entry['provenance']),2)
        self.assertEqual(entry['content_sha256'],sha(out[entry['path']]))
        self.assertGreater(retrieve_bm25('pytest',out,validate_index(out))[0].score,0)

    def test_successful_independent_review_and_usage(self):
        out,receipt,models=self.run_stage()
        self.assertEqual(receipt['status'],'consolidated'); self.assertNotEqual(digest(out),digest(self.files))
        self.assertEqual(len(models.requests),2)
        self.assertTrue(models.requests[1][0]['content'].startswith('Independently'))
        self.assertEqual(read_json(self.root/'stage/usage.json')['total']['input_tokens'],20)
        self.assertTrue(all(o['num_retries']==0 for o in models.options))

    def test_different_conditions_keep_savepoint(self):
        self.files['references/b.md']=self.files['references/b.md'].replace('When pytest fails, inspect evidence.','When pytest isolation fails, inspect shared state.')
        self.files[INDEX_PATH]=build_index(self.files)
        out,receipt,_=self.run_stage()
        self.assertEqual(out,self.files); self.assertEqual(receipt['status'],'consolidation_failed')
        self.assertIn('Different applicability',receipt['reason'])
        self.assertEqual(receipt['decision'],'KEEP_SEPARATE')

    def test_metadata_exclusion_difference_rejects_even_identical_body(self):
        self.files=sample({'references/a.md':'# Same\npytest\n','references/b.md':'# Same\npytest\n'},
                          {'references/b.md':{'exclude_when':['SQLFluff']}})
        with self.assertRaises(c.ConsolidationRejected): self.apply(plan_for(self.files,'DEDUPLICATE',target='references/a.md'))

    def test_conflict_verdict_preserves_both_files(self):
        out,receipt,_=self.run_stage(reviewer=lambda data:{'approved':False,'conflicts':['Contradictory steps'],'uncertainties':[],'reason':'Keep separate'})
        self.assertEqual(out,self.files); self.assertEqual(receipt['status'],'consolidation_failed')

        self.assertEqual(receipt['decision'],'CONFLICT_UNRESOLVED')

    def test_explicit_conflict_and_keep_decisions(self):
        for operation in ('KEEP_SEPARATE','CONFLICT_UNRESOLVED'):
            out,receipt,models=self.run_stage(plan_for(self.files,operation),name=operation)
            self.assertEqual(out,self.files); self.assertEqual(receipt['status'],'consolidation_skipped')
            self.assertEqual(len(models.requests),1)

    def test_exact_deduplicate_keeps_evidence_union(self):
        self.files=sample({'references/a.md':'# Same\npytest\n','references/b.md':'# Same\npytest\n'})
        out,_=self.apply(plan_for(self.files,'DEDUPLICATE',target='references/a.md'))
        self.assertIn('references/a.md',out); self.assertNotIn('references/b.md',out)

    def test_partial_duplicate_cannot_delete_unique_rule(self):
        self.files=sample({'references/a.md':'# Same\npytest\n','references/b.md':'# Same\npytest\nUnique exception.\n'})
        with self.assertRaises(c.ConsolidationRejected): self.apply(plan_for(self.files,'DEDUPLICATE',target='references/a.md'))

    def test_split_reuses_p1_coverage_and_retains_navigation(self):
        self.files=sample({'references/a.md':'# Click\n'+section('Click first',size=1200)+section('Click second',size=1200)})
        out,ledger=self.apply(plan_for(self.files,'SPLIT'))
        self.assertIn('references/a.md',out);self.assertEqual(len(validate_index(out)['entries']),3)
        self.assertEqual(len(ledger['records']),2)
        self.assertTrue(all(len(t)<=5000 for p,t in out.items() if p.startswith('references/')))

    def test_merge_overflow_splits_whole_topics(self):
        self.files=sample({'references/a.md':'# First\n'+section('Click first',size=3500),
                          'references/b.md':'# Second\n'+section('Click second',size=3500)})
        out,ledger=self.apply()
        self.assertTrue(ledger['overflow_split_records'])
        self.assertTrue(all(len(t)<=5000 for p,t in out.items() if p.startswith('references/')))

    def test_indivisible_merge_overflow_rejected(self):
        self.files=sample({'references/a.md':'# A\n'+'pytest '*500+'\n','references/b.md':'# B\n'+'pytest '*500+'\n'})
        with self.assertRaises(ValueError):self.apply()

    def test_root_and_reference_links_repaired(self):
        self.files['SKILL.md']+='\nRead [A](references/a.md) and [B](references/b.md#b).\n'
        self.files['references/other.md']='# Other\nSee [A](a.md).\n'; self.files[INDEX_PATH]=build_index(self.files)
        out,ledger=self.apply(plan_for(self.files,paths=['references/a.md','references/b.md']))
        self.assertIn('(references/merged.md#b)',out['SKILL.md'])
        self.assertIn('(merged.md)',out['references/other.md']);self.assertEqual(len(ledger['links']),2)

    def test_bare_and_code_old_paths_reject(self):
        for text in ('Use references/a.md.','`[A](references/a.md)`','```bash\ncat references/a.md\n```'):
            original=self.files['SKILL.md']; self.files['SKILL.md']+= '\n'+text+'\n'
            with self.subTest(text=text),self.assertRaises(ValueError):self.apply()
            self.files['SKILL.md']=original

    def test_duplicate_heading_anchors_reject_merge(self):
        self.files=sample({'references/a.md':'# Same\nA\n','references/b.md':'# Same\nB\n'})
        with self.assertRaises(c.ConsolidationRejected):self.apply()

    def test_bad_hash_span_coverage_and_rewrite_rejected(self):
        plan=plan_for(self.files)
        variants=[]
        p=copy.deepcopy(plan);p['sources'][0]['sha256']='0'*64;variants.append(p)
        p=copy.deepcopy(plan);p['sources'][0]['end']-=1;variants.append(p)
        p=copy.deepcopy(plan);p['coverage'].pop();variants.append(p)
        p=copy.deepcopy(plan);p['targets']['references/merged.md']='summary';variants.append(p)
        p=copy.deepcopy(plan);p['reference_changes']=[];variants.append(p)
        p=copy.deepcopy(plan);p['targets']={'../escape.md':'bad'};variants.append(p)
        for p in variants:
            with self.subTest(plan=p),self.assertRaises(ValueError):self.apply(p)

    def test_invalid_json_retries_bounded_then_savepoint(self):
        out,receipt,models=self.run_stage('not JSON')
        self.assertEqual(out,self.files);self.assertEqual(len(models.requests),3)
        self.assertEqual(receipt['status'],'consolidation_failed')

    def test_timeout_optional_but_disk_error_aborts(self):
        def timeout(data):raise TimeoutError('model timeout')
        models=PlannerModels(planner=timeout)
        out=c.consolidate(self.files,self.config,self.root/'timeout',model_factory=models)
        self.assertEqual(out,self.files)
        def disk(data):raise OSError('disk failed')
        with self.assertRaises(OSError):c.consolidate(self.files,self.config,self.root/'disk',model_factory=PlannerModels(planner=disk))

    def test_index_runtime_error_aborts(self):
        with patch.object(c,'build_index',side_effect=RuntimeError('index generator failed')),self.assertRaises(RuntimeError):self.run_stage()

    def test_input_and_call_budget_enforced(self):
        self.config['reference_maintenance']['max_input_chars']=1
        out,_,models=self.run_stage();self.assertEqual(out,self.files);self.assertEqual(models.requests,[])
        self.config['reference_maintenance'].update(max_input_chars=120000,max_llm_calls=2,max_rounds=3)
        out,_,models=self.run_stage('invalid',name='calls');self.assertEqual(len(models.requests),2)

    def test_modified_file_cap_reverts_optional_batch(self):
        out,receipt,_=self.run_stage() # baseline success
        self.config['reference_maintenance']['max_modified_files']=1
        out,receipt,_=self.run_stage(name='limited');self.assertEqual(out,self.files)
        self.assertIn('Modified-file',receipt['reason'])

    def test_savepoint_write_failure_aborts_before_model(self):
        with patch.object(c,'write_json',side_effect=OSError('full disk')),self.assertRaises(OSError):self.run_stage()

    def test_immutable_incoming_reference_rejects_optional_move(self):
        self.files['scripts/inspect_project.py'] += '\n# Read references/a.md\n'
        with self.assertRaisesRegex(ValueError,'immutable'):self.apply()

    def test_index_hashes_unique_ids_and_search_after_split(self):
        self.files=sample({'references/a.md':'# Click\n'+section('Click first',size=1200)+section('Click second',size=1200)})
        out,_=self.apply(plan_for(self.files,'SPLIT'))
        entries=validate_index(out)['entries']
        self.assertEqual(len({e['id'] for e in entries}),len(entries))
        for e in entries:
            self.assertEqual(e['char_count'],len(out[e['path']]))
            self.assertEqual(e['content_sha256'],sha(out[e['path']]))
        self.assertTrue(any('split-' in item.path for item in retrieve_bm25('click Option caller',out,validate_index(out))))


class ConsolidationCheckpointTests(unittest.TestCase):
    def setUp(self):
        p0.ManagedCheckpointTests.setUp(self)
        self.config.update(skill_maintenance={'enabled':True}, reference_maintenance={'enabled':True})
        self.tasks=[{'id':f'task-{i:02d}','benchmark':'swe','prompt':'Fix visible issue'} for i in range(1,11)]
        self.seed=sample()
        self.source=SkillStore(Path(self.tmp.name)/'source',self.seed,directory_schema=1).current()
        self.config['initial_skill']=str(self.source)
        self.planner=PlannerModels(planner=self.make_plan)
        self.mode='merge'
        def factory(options):
            basic=self.models(options); stage=self.planner(options)
            query,stats=basic.query,basic.stats
            def dispatch(messages,**kwargs):
                if messages[0]['content'].startswith(('Propose ONE bounded','Independently review this lossless')):
                    stage.deadline=basic.deadline
                    return stage.query(messages,**kwargs)
                return query(messages,**kwargs)
            basic.query=dispatch
            basic.stats=lambda:{k:stats()[k]+stage.stats()[k] for k in stats()}
            return basic
        self.factory=factory
        mock=patch('rsi.agent.runner.evolve_skill',side_effect=self.learn)
        mock.start();self.addCleanup(mock.stop)

    def learn(self,task,results,skill,*args,**kwargs):
        text=(skill/'SKILL.md').read_text(encoding='utf-8')
        return {'changes':{'SKILL.md':text+'\nVerify task '+task['id']+' callers.\n'},
                'reason':'Mock effective learning','evidence_rollouts':list(range(5))}

    def make_plan(self,data):
        if self.mode=='invalid':return 'invalid JSON'
        if self.mode=='interrupt':raise KeyboardInterrupt('interrupted planner')
        seed=next(read_json(p)['files'] for p in self.output.glob('task-*/consolidation/savepoint.json') if read_json(p)['version']==data['input_hash'])
        return plan_for(seed,'KEEP_SEPARATE' if self.mode=='keep' else 'MERGE')

    def run_tasks(self,resume=False):
        return run_experiment(self.tasks,self.config,self.output,resume=resume,
                              backend_factory=Backend,model_factory=self.factory)

    def checkpoint(self):return read_json(self.output/'checkpoint.json')

    def test_tenth_commit_success_shared_five_routes_and_resume(self):
        self.run_tasks(); state=self.checkpoint()
        self.assertEqual(state['reference_maintenance'],{'accepted_updates':10,'checks':1,'cursor':1})
        self.assertEqual(len(self.planner.requests),2)
        self.assertTrue(all(not e['update']['reference_maintenance']['due'] for e in state['completed'][:9]))
        update=state['completed'][-1]['update']
        self.assertEqual(update['consolidation']['status'],'consolidated')
        self.assertNotEqual(update['learning_skill_version'],update['after_skill_version'])
        self.assertEqual(len(validate_index(read_skill_directory(self.output/'skills/versions'/update['after_skill_version'],directory_schema=1))['entries']),1)
        for i in range(5):
            trajectory=read_json(self.output/f'task-10/rollout-{i}/trajectory.json')
            self.assertEqual(trajectory['skill_version'],update['before_skill_version'])
        before=len(self.models.requests),len(self.planner.requests)
        self.run_tasks(resume=True);self.run_tasks(resume=True)
        self.assertEqual(before,(len(self.models.requests),len(self.planner.requests)))
        self.assertEqual(read_skill_directory(self.source,directory_schema=1),self.seed)

    def test_tenth_optional_failure_keeps_learning_commit(self):
        self.mode='invalid';self.run_tasks();state=self.checkpoint()
        update=state['completed'][-1]['update']
        self.assertTrue(update['accepted'])
        self.assertEqual(update['after_skill_version'],update['learning_skill_version'])
        self.assertEqual(update['consolidation']['status'],'consolidation_failed')
        self.assertEqual(state['reference_maintenance']['accepted_updates'],10)
        self.assertEqual(len(self.planner.requests),3)
        self.run_tasks(resume=True);self.assertEqual(len(self.planner.requests),3)

    def test_interrupt_before_trigger_then_resume(self):
        with patch.object(c,'consolidate',side_effect=KeyboardInterrupt('before stage')),self.assertRaises(KeyboardInterrupt):self.run_tasks()
        self.assertEqual(self.checkpoint()['reference_maintenance']['accepted_updates'],9)
        self.assertEqual(self.planner.requests,[])
        self.run_tasks(resume=True)
        self.assertEqual(self.checkpoint()['reference_maintenance']['accepted_updates'],10)
        self.assertTrue(list((self.output/'attempts').glob('task-10-*')))

    def test_interrupt_during_plan_does_not_reuse_incomplete_attempt(self):
        self.mode='interrupt'
        with self.assertRaises(KeyboardInterrupt):self.run_tasks()
        self.assertEqual(self.checkpoint()['reference_maintenance']['checks'],0)
        self.mode='merge';self.run_tasks(resume=True)
        self.assertEqual(len(self.planner.requests),3)
        self.assertEqual(self.checkpoint()['reference_maintenance']['checks'],1)
        archived=list((self.output/'attempts').glob('task-10-*'))
        self.assertEqual(len(archived),1)
        self.assertTrue((archived[0]/'consolidation/savepoint.json').exists())

    def test_validated_before_commit_crash_retries_without_double_count(self):
        real=checkpoints.commit
        def fail(output,state,report,update):
            if len(state['completed'])==9:raise OSError('before commit')
            return real(output,state,report,update)
        with patch('rsi.agent.runner.commit',side_effect=fail),self.assertRaises(OSError):self.run_tasks()
        self.assertEqual(self.checkpoint()['reference_maintenance']['accepted_updates'],9)
        self.assertEqual(len(self.planner.requests),2)
        self.run_tasks(resume=True)
        self.assertEqual(len(self.planner.requests),4)
        self.assertEqual(self.checkpoint()['reference_maintenance']['checks'],1)

    def test_commit_then_publication_crash_replays_without_models(self):
        real=checkpoints.publish
        def fail(output,state):
            if len(state['completed'])==10:raise OSError('publication failed')
            return real(output,state)
        with patch.object(checkpoints,'publish',side_effect=fail),self.assertRaises(OSError):self.run_tasks()
        state=self.checkpoint();self.assertEqual(state['reference_maintenance']['accepted_updates'],10)
        before=len(self.models.requests),len(self.planner.requests)
        self.run_tasks(resume=True)
        self.assertEqual(before,(len(self.models.requests),len(self.planner.requests)))
        self.assertEqual(read_json(self.output/'skills/current.json')['version'],state['current_skill_version'])

    def test_corrupted_counter_and_receipt_rejected(self):
        self.run_tasks();state=self.checkpoint()
        bad=copy.deepcopy(state);bad['reference_maintenance']['accepted_updates']=11
        write_json(self.output/'checkpoint.json',bad)
        with self.assertRaisesRegex(ValueError,'counter'):checkpoints.resume_request(self.output)
        write_json(self.output/'checkpoint.json',state)
        path=self.output/'task-10/consolidation/coverage.json'
        data=read_json(path);data['records']=[];write_json(path,data)
        with self.assertRaisesRegex(ValueError,'artifact hash'):checkpoints.resume_request(self.output)

    def test_no_change_unchanged_and_rejected_do_not_count(self):
        self.tasks=self.tasks[:1]
        for name,proposal in [('empty',{'changes':{},'reason':'none'}),
            ('same',{'changes':{'SKILL.md':self.seed['SKILL.md']},'reason':'same','evidence_rollouts':list(range(5))}),
            ('rejected',{'changes':{'scripts/inspect_project.py':'bad'},'reason':'bad','evidence_rollouts':list(range(5))})]:
            self.output=Path(self.tmp.name)/name
            with patch('rsi.agent.runner.evolve_skill',return_value=proposal):self.run_tasks()
            self.assertEqual(self.checkpoint()['reference_maintenance'],initial_state())
            self.assertFalse(self.checkpoint()['completed'][0]['update']['accepted'])
        self.assertEqual(self.planner.requests,[])

    def test_fixed_evaluation_does_not_count(self):
        import threading
        self.tasks=self.tasks[:1];self.config.update(evolve=False,rollouts=1,execution_mode="single")
        Backend.barrier=threading.Barrier(1)
        self.run_tasks()
        self.assertEqual(self.checkpoint()['reference_maintenance'],initial_state())
        self.assertEqual(self.planner.requests,[])

    def test_twentieth_committed_update_checks_again(self):
        self.mode='keep'
        self.tasks=[{'id':f'task-{i:02d}','benchmark':'swe','prompt':'Fix visible issue'} for i in range(1,21)]
        self.run_tasks();state=self.checkpoint()
        self.assertEqual([i for i,e in enumerate(state['completed'],1) if e['update']['reference_maintenance']['due']],[10,20])
        self.assertEqual(state['reference_maintenance'],{'accepted_updates':20,'checks':2,'cursor':2})
        self.assertEqual(len(self.planner.requests),2)
        self.run_tasks(resume=True);self.assertEqual(len(self.planner.requests),2)

    def test_mandatory_maintenance_failure_does_not_count(self):
        from rsi.skills.maintenance import maintain_candidate, MaintenanceRejected
        self.tasks=self.tasks[:1]
        def fail(files,changes,config,folder,**kwargs):
            if Path(folder).name=='maintenance':raise MaintenanceRejected('mandatory failure')
            return maintain_candidate(files,changes,config,folder,**kwargs)
        with patch('rsi.agent.runner.maintain_candidate',side_effect=fail):self.run_tasks()
        self.assertEqual(self.checkpoint()['reference_maintenance'],initial_state())
        self.assertFalse(self.checkpoint()['completed'][0]['update']['accepted'])

    def test_final_version_disk_failure_aborts_tenth_commit(self):
        real=SkillStore.materialize
        def fail(store,files):
            if 'references/merged.md' in files:raise OSError('version materialization failed')
            return real(store,files)
        with patch.object(SkillStore,'materialize',fail),self.assertRaises(OSError):self.run_tasks()
        self.assertEqual(self.checkpoint()['reference_maintenance']['accepted_updates'],9)
        self.run_tasks(resume=True)
        self.assertEqual(self.checkpoint()['reference_maintenance']['accepted_updates'],10)
