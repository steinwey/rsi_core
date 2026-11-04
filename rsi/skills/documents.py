"""Bounded document consistency checks, not a solver-performance evaluator."""
import posixpath
import re
from urllib.parse import unquote, urlsplit

CHECKS = ['local_document_links', 'probe_retest_consistency']


def document_lines(text):
    """Exclude fenced examples from instruction/link checks."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)[0]
            if fence is None:
                fence = token
            elif fence == token:
                fence = None
            continue
        if fence is None:
            yield number, line


def consistency_issues(files):
    issues, retests, bans = [], [], []
    for name, text in sorted(files.items()):
        if name != 'SKILL.md' and not (name.startswith('references/') and name.endswith('.md')):
            continue
        for number, line in document_lines(text):
            for raw in re.findall(r'(?<!!)\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)', line):
                target = unquote(raw.strip('<>'))
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                location = posixpath.normpath(posixpath.join(posixpath.dirname(name), parsed.path))
                if (location.startswith(('../', '/')) or '\\' in location
                        or location not in files):
                    issues.append(f'{name}:{number}: missing or outside-Skill link target {target!r}')
            lower = line.lower()
            if re.search(r'\b(?:run|execute)\b.*\bprobes?\b.*\bbefore\b.*\bafter\b', lower):
                if not re.search(r'\b(?:do not|never)\b.*\bprobes?\b', lower):
                    retests.append(f'{name}:{number}')
            blanket = re.search(r'\b(?:never|do not|must not)\b[^\n]*\b(?:same|identical)\b'
                                r'[^\n]*\bprobe(?:s| script)?\b[^\n]*\b(?:more than once|twice|again)\b', lower)
            if blanket and not re.search(r'\b(?:unless|except|unchanged|unmodified)\b|'
                                         r'\bwithout\b[^.]*\b(?:change|patch|modification)', lower):
                bans.append(f'{name}:{number}')
    if retests and bans:
        issues.append('Conflicting probe instructions: before/after-patch retesting at '
                      + ', '.join(retests) + ' conflicts with an unconditional repeated-probe ban at '
                      + ', '.join(bans) + '. Permit retesting after source/input changes; prohibit only unchanged no-progress repetition.')
    return issues


def validate_consistency(files):
    issues = consistency_issues(files)
    if issues:
        raise ValueError('Document consistency failed: ' + '; '.join(issues))
