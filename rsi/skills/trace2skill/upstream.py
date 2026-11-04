"""Load the vendored upstream without installing its spreadsheet benchmark runner."""
import hashlib
import sys
from functools import lru_cache
from rsi import ROOT
from rsi.core.common import read_json

SOURCE = ROOT / 'vendor/Trace2Skill-upstream'


@lru_cache(maxsize=1)
def load():
    manifest = read_json(SOURCE / 'rsi-source-manifest.json')
    expected = read_json(ROOT / 'upstreams.lock.json')['Trace2Skill']['commit']
    if manifest['commit'] != expected:
        raise RuntimeError('Trace2Skill version differs from upstream lock')
    for name, checksum in manifest['files'].items():
        if hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() != checksum:
            raise RuntimeError(f'Trace2Skill source changed: {name}')
    sys.path.insert(0, str(SOURCE))
    from src.react_agent import ReActAgent, AgentConfig, Tool, ToolParameter
    from src.react_agent.models import Message
    from skill_evolver.parallel_success_evolving_agent import CombinedParallelSkillEvolver
    from skill_evolver.parallel_evolving_agent import _find_section_bounds, _apply_patch_edit_to_content
    return ReActAgent, AgentConfig, Tool, ToolParameter, Message, CombinedParallelSkillEvolver, _find_section_bounds, _apply_patch_edit_to_content
