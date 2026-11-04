"""Pinned, offline proxy BPE. No claim of target-model tokenizer equivalence."""
import base64
import hashlib
import importlib.metadata
import json
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from rsi import ROOT
from rsi.skills.budget import BudgetError, CounterInfo, UNSUPPORTED_MODELS

VERSION = '0.14.0'
ENCODING = 'o200k_base'
METHOD = 'o200k_proxy_v1'
VOCAB_URL = 'https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'
VOCAB_SHA256 = '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'
CACHE_PATH = ROOT / '.cache/skill-tokenizers/o200k_base.tiktoken'
# o200k_base regex from OpenAI tiktoken 0.14.0, MIT. See tokenizer NOTICE.
PATTERN = '|'.join([
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r'\p{N}{1,3}', r' ?[^\s\p{L}\p{N}]+[\r\n/]*', r'\s*[\r\n]+', r'\s+(?!\S)', r'\s+',
])
FIELDS = {'method', 'model', 'provider', 'api_base', 'tokenizer_version', 'encoding', 'safety_margin'}


def validate_estimated(model_config, options):
    if not isinstance(options, dict) or set(options) != FIELDS:
        raise ValueError('estimated mode requires an explicit complete estimated_counter configuration')
    if options['method'] != METHOD or options['encoding'] != ENCODING or options['tokenizer_version'] != VERSION:
        raise ValueError('Estimated counter requires o200k_proxy_v1, o200k_base and tiktoken 0.14.0')
    if (not isinstance(options['model'], str) or options['model'] not in UNSUPPORTED_MODELS
            or options['model'] != model_config.get('name')):
        raise ValueError('Unsupported or mismatched estimated counter model')
    for key in ('provider', 'api_base'):
        if not isinstance(options[key], str) or not options[key] or options[key] != model_config.get(key):
            raise ValueError(f'Estimated counter {key} must match the solver configuration')
    margin = options['safety_margin']
    if type(margin) not in (int, float) or not 0 <= margin <= 10:
        raise ValueError('safety_margin must be finite and between 0 and 10 (fractional multiplier)')
    return dict(options)


def effective_limit(budget, margin):
    # Decimal avoids a binary-float off-by-one at exact integer boundaries.
    return int((Decimal(budget) / (1 + Decimal(str(margin)))).to_integral_value(rounding=ROUND_FLOOR))


def load_encoding(path=None):
    try:
        actual = importlib.metadata.version('tiktoken')
    except importlib.metadata.PackageNotFoundError as exc:
        raise BudgetError('counter_unavailable', 'Install the routing extra: pip install -e ".[routing]"') from exc
    if actual != VERSION:
        raise BudgetError('counter_unavailable', f'Requires tiktoken {VERSION}; installed {actual}. No automatic substitution.')
    location = Path(path) if path is not None else CACHE_PATH
    try:
        data = location.read_bytes()
    except OSError as exc:
        raise BudgetError('counter_unavailable', 'Missing local vocabulary. Run python -m rsi.skills.tokenizer_setup first.') from exc
    if hashlib.sha256(data).hexdigest() != VOCAB_SHA256:
        raise BudgetError('counter_unavailable', 'Local tokenizer vocabulary SHA256 mismatch; refusing to use it.')
    # Pure local parsing: do not call get_encoding or URL-aware tiktoken loaders.
    ranks = {base64.b64decode(token, validate=True): int(rank)
             for token, rank in (line.split() for line in data.splitlines() if line)}
    import tiktoken
    return tiktoken.Encoding(name=ENCODING, pat_str=PATTERN, mergeable_ranks=ranks,
                             special_tokens={'<|endoftext|>': 199999, '<|endofprompt|>': 200018})


class EstimatedCounter:
    def __init__(self, model_config, options):
        options = validate_estimated(model_config, options)
        self._encoding = load_encoding()
        manifest = {'method': METHOD, 'version': VERSION, 'encoding': ENCODING,
                    'vocabulary_sha256': VOCAB_SHA256,
                    'pattern_sha256': hashlib.sha256(PATTERN.encode()).hexdigest(),
                    'model': options['model'], 'provider': options['provider'],
                    'endpoint_sha256': hashlib.sha256(options['api_base'].encode()).hexdigest()}
        self.info = CounterInfo(options['model'], json.dumps(manifest, sort_keys=True, separators=(',', ':')),
            'estimated', 'Exact count only for the pinned o200k_base proxy vocabulary, NOT the target API model. '
            'Mapping and provider usage are unverified; error is unbounded. Safety margin is a heuristic, '
            'not a guarantee. Counts SkillContext text only, excluding chat framing and other messages.')

    def count(self, text):
        # Treat special-token-looking text literally, as ordinary Skill prose.
        return len(self._encoding.encode_ordinary(text))
