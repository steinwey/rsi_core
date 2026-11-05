"""Opt-in verified snapshots; mount enforcement is distinct from copy auditing."""
import hashlib
import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from rsi.core.common import digest, write_json
from rsi.skills.store import load_initial_skill, SKILL_ROOT

DEFAULTS = dict(enabled=False, install_mode='auto', require_read_only=False,
                expose_index=True, audit_reads=True, record_observation_cost=True,
                verify_snapshot_before=True, verify_snapshot_after=True)


def settings(config):
    block = config.get('skill_access', {})
    if not isinstance(block, dict) or block.keys() - DEFAULTS.keys():
        raise ValueError('Invalid skill_access configuration')
    value = DEFAULTS | block
    for key in DEFAULTS.keys() - {'install_mode'}:
        if type(value[key]) is not bool:
            raise ValueError('skill_access.' + key + ' must be boolean')
    if value['install_mode'] not in ('auto', 'read_only', 'copy_audited'):
        raise ValueError('Invalid skill_access.install_mode')
    if value['enabled']:
        if (config.get('skill_directory', {}).get('schema') != 1
                or config.get('skill_update_method', 'trace2skill') != 'trace2skill'
                or not config.get('skill_routing', {}).get('enabled')):
            raise ValueError('Skill access requires managed Trace2Skill with P2 routing enabled')
        if not value['verify_snapshot_before'] or not value['verify_snapshot_after']:
            raise ValueError('Enabled skill access requires before/after integrity verification')
        if value['require_read_only'] and value['install_mode'] == 'copy_audited':
            raise ValueError('Required read-only cannot use copy_audited')
    return value


def guidance(options):
    if not options or not options['enabled']:
        return ''
    discover = 'Inspect routing/index.yaml and read relevant references using Bash. ' if options['expose_index'] else 'Read relevant linked references using Bash. '
    return ('\n## Autonomous Skill access\nIf the loaded guidance does not cover a task-specific problem, '
            + discover + 'Read only files needed for this task; automatically loaded files are listed above. '
            'Do not modify /opt/rsi-skills/. Access auditing is best effort, not an additional tool.\n')


def manifest(files):
    return {p: hashlib.sha256(t.encode('utf-8')).hexdigest() for p,t in sorted(files.items())}


def manifest_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class Snapshot:
    def __init__(self, skill):
        self.files, _ = load_initial_skill(skill, directory_schema=1)
        if digest(self.files) != Path(skill).name:
            raise ValueError('Skill access version mismatch')
        self.temporary = tempfile.TemporaryDirectory(prefix='rsi-access-')
        self.path = Path(self.temporary.name) / Path(skill).name
        try:
            for name,text in self.files.items():
                target=self.path/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(text.encode('utf-8'))
        except BaseException:
            self.cleanup();raise
        self.expected=manifest(self.files)
    def cleanup(self):
        self.temporary.cleanup()


def docker_args(snapshot):
    source=str(Path(snapshot).resolve())
    if any(c in source for c in ',\n\r'):
        raise ValueError('Unsupported Docker bind source punctuation')
    return ['--mount',f'type=bind,source={source},target={SKILL_ROOT},readonly',
            '--cap-drop=ALL','--security-opt=no-new-privileges:true']


def remote_json(env, script, marker):
    for python in ('python','python3'):
        response=env.execute(python+' -c '+shlex.quote(script))
        if response['returncode']==0:
            lines=[v[len(marker):] for v in response['output'].splitlines() if v.startswith(marker)]
            if len(lines)==1:
                return json.loads(lines[0])
    raise RuntimeError('Cannot verify installed Skill: '+response.get('output','')[:500])


def verify(env, expected):
    script="""import pathlib,hashlib,json,stat
root=pathlib.Path('/opt/rsi-skills')
assert root.is_dir() and not any(p.is_symlink() for p in [root,*root.parents])
paths=list(root.rglob('*'))
assert not any(p.is_symlink() for p in paths)
assert all(p.is_dir() or stat.S_ISREG(p.stat().st_mode) for p in paths)
actual={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
print('RSI_ACCESS_MANIFEST='+json.dumps(actual,sort_keys=True))
"""
    actual=remote_json(env,script,'RSI_ACCESS_MANIFEST=')
    if actual != expected:
        raise RuntimeError('Skill snapshot integrity mismatch (missing, changed or extra files)')
    return manifest_hash(actual)


def verify_read_only(env):
    info=json.loads(subprocess.run([env.config.executable,'inspect',env.container_id],
        check=True,capture_output=True,text=True,encoding='utf-8',timeout=30).stdout)[0]
    mounts=[m for m in info['Mounts'] if m['Destination']==SKILL_ROOT]
    host=info['HostConfig']
    if (len(mounts)!=1 or mounts[0].get('RW') is not False or mounts[0]['Type']!='bind'
            or 'ALL' not in (host.get('CapDrop') or [])
            or not any(s.startswith('no-new-privileges') for s in host.get('SecurityOpt') or [])):
        raise RuntimeError('Docker read-only mount/capability verification failed')
    # Only the disposable private copy is mounted, never the SkillStore itself.
    script="""import pathlib,json,errno
root=pathlib.Path('/opt/rsi-skills'); p=root/'SKILL.md'
def overwrite(): p.write_text('probe')
def append():
    with p.open('a') as f: f.write('probe')
checks={}
for name,op in [('create',lambda:(root/'.rsi-probe').write_text('probe')),('overwrite',overwrite),('modify',append),('delete',p.unlink),('rename',lambda:p.rename(root/'renamed.md'))]:
    try: op(); checks[name]=False
    except OSError as e: checks[name]=e.errno==errno.EROFS
print('RSI_READ_ONLY='+json.dumps(checks))
"""
    probes=remote_json(env,script,'RSI_READ_ONLY=')
    if set(probes)!={'create','overwrite','modify','delete','rename'} or not all(v is True for v in probes.values()):
        raise RuntimeError('Docker write-denial probes failed')
    return probes


def install(env, snapshot, options, folder):
    from rsi.benchmarks.backends import install_skill
    record={'skill_version':snapshot.path.name,'expected_manifest':snapshot.expected,
            'snapshot_sha256':manifest_hash(snapshot.expected),'read_only_enforced':False,'status':'failed'}
    try:
        mounted=getattr(env,'rsi_read_only_mount',False) is True
        if mounted:
            record['write_probes']=verify_read_only(env)
            record.update(install_mode='read_only',read_only_enforced=True,degraded=False)
        else:
            if options['install_mode']=='read_only' or options['require_read_only']:
                raise RuntimeError('Backend lacks verified strong read-only capability; downgrade forbidden')
            record.update(install_mode='copy_audited',degraded=True,
                          limitation='Writable copy; endpoint hashes cannot exclude transient or concealed changes')
            install_skill(env,snapshot.path,directory_schema=1)
        record['installed_snapshot_sha256']=verify(env,snapshot.expected)
        record['status']='verified'
        return record
    finally:
        write_json(Path(folder)/'skill-install.json',record)


class ManagedBackend:
    """Keep the bind source alive until the existing backend lifecycle closes it."""
    def __init__(self, backend, snapshot):
        self.backend,self.snapshot=backend,snapshot
    def __getattr__(self,name):return getattr(self.backend,name)
    def close(self):
        self.backend.close()
        self.snapshot.cleanup()


def receipts(folder, results):
    """Bind security receipts to the task checkpoint; optional telemetry can be unknown."""
    root=Path(folder)
    files={}
    for result in results:
        sub=root/f"rollout-{result['rollout']}"
        for name in ('skill-install.json','skill-access-summary.json','skill-access.jsonl'):
            path=sub/name
            if path.exists():
                files[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def verify_receipt(output, report, config):
    from rsi.core.common import read_json, safe_id
    from rsi.skills.directory import reject_links
    if not settings(config)['enabled']:
        return
    root=Path(output)/safe_id(report['task_id'])
    block=report.get('skill_access',{})
    artifacts=block.get('artifacts',{})
    allowed={f'rollout-{i}/{name}' for i in range(config['rollouts'])
             for name in ('skill-install.json','skill-access-summary.json','skill-access.jsonl')}
    required={f'rollout-{i}/skill-install.json' for i in range(config['rollouts'])}
    if not isinstance(artifacts,dict) or not required<=artifacts.keys() or artifacts.keys()-allowed:
        raise ValueError('Missing/invalid Skill access receipts')
    for name,checksum in artifacts.items():
        path=root/name;reject_links(path)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=checksum:
            raise ValueError('Skill access receipt hash mismatch')
    for name in required:
        receipt=read_json(root/name)
        options=settings(config)
        if (receipt.get('skill_version')!=report['skill_version'] or receipt.get('status')!='verified'
                or receipt.get('final_status')!='verified'
                or receipt.get('installed_snapshot_sha256')!=receipt.get('snapshot_sha256')
                or receipt.get('final_snapshot_sha256')!=receipt.get('snapshot_sha256')
                or ((options['require_read_only'] or options['install_mode']=='read_only') and receipt.get('read_only_enforced') is not True)):
            raise ValueError('Unverified Skill access snapshot')
        files,_=load_initial_skill(Path(output)/'skills/versions'/report['skill_version'],directory_schema=1)
        if receipt.get('expected_manifest')!=manifest(files) or receipt.get('snapshot_sha256')!=manifest_hash(manifest(files)):
            raise ValueError('Skill access snapshot identity mismatch')
