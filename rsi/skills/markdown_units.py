"""Conservative, span-preserving Markdown sections for lossless migration.

Lists, tables, quotes and fences are never split internally. Only leaf sections
move; ancestor introductions are copied as context and remain at their source.
"""
import hashlib
import posixpath
import re
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit, unquote

import yaml


class UnsafeMarkdown(ValueError):
    pass


def canonical(text):
    if not isinstance(text, str):
        raise UnsafeMarkdown('Document must be text')
    return text.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n').rstrip('\n') + '\n'


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


CORE_TITLE = re.compile(r'(?i)^(?:protected core|core|general(?: principles| workflow)?|workflow|'
                        r'principles|safety|verification|validation|compatibility|'
                        r'toolkit|tools|skill access|using references|通用原则|全局流程|验证约束|辅助库读取)$')
GLOBAL = re.compile(r'(?i)\b(?:for all tasks|for every task|regardless of (?:task|framework)|'
                    r'never infer success|do not infer success|global invariant|protected core)\b|跨任务通用|全局约束')
SCOPE = re.compile(r'`[^`\n]+`|\b(?:Click|SQLFluff|pytest|Django|Flask|Python|pdb|argparse|'
                   r'NumPy|pandas|TypeError|ValueError)\b|\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b')
CONDITION = re.compile(r'(?i)^\s*(?:(?:when|if|for|only when|in)\b|当|如果|对于).*')
LINK = re.compile(r'(!?\[[^\]\n]*\])\(([^()\s]+)\)')


@dataclass(frozen=True)
class Unit:
    id: str
    start: int
    end: int
    heading_end: int
    heading_path: tuple
    content_sha256: str
    text: str
    context: str
    protected: bool
    applicability: tuple

    def record(self):
        return asdict(self)


def _scan(text):
    lines = text.splitlines(keepends=True)
    positions, offset = [], 0
    for line in lines:
        positions.append(offset)
        offset += len(line)
    start = 0
    if lines and lines[0].strip() == '---':
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
        if end is None:
            raise UnsafeMarkdown('Unclosed YAML frontmatter')
        try:
            meta = yaml.safe_load(''.join(lines[1:end]))
        except yaml.YAMLError as exc:
            raise UnsafeMarkdown('Invalid frontmatter') from exc
        if not isinstance(meta, dict):
            raise UnsafeMarkdown('Frontmatter must be a mapping')
        start = end + 1
    outside, fence, width = set(), None, 0
    for i in range(start, len(lines)):
        line = lines[i].rstrip('\n')
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            if marker and marker[1][0] == fence and len(marker[1]) >= width and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence, width = marker[1][0], len(marker[1])
            continue
        if not line.startswith(('    ', '\t')):
            outside.add(i)
    if fence:
        raise UnsafeMarkdown('Unclosed fenced code block')
    headings = []
    consumed = set()
    for i in sorted(outside):
        if i in consumed:
            continue
        line = lines[i].rstrip('\n')
        match = re.match(r'^(#{1,6})[ \t]+(.+?)\s*#*$', line)
        if match:
            headings.append((positions[i], positions[i] + len(lines[i]), len(match[1]), match[2]))
        elif (i + 1 in outside and line and not line[0].isspace()
              and not re.match(r'^(?:>|[-*+] |\d+[.)] )', line)
              and re.fullmatch(r'(?:={3,}|-{3,})\s*', lines[i+1].rstrip('\n'))):
            level = 1 if lines[i+1].startswith('=') else 2
            headings.append((positions[i], positions[i+1] + len(lines[i+1]), level, line))
            consumed.add(i + 1)
    return lines, outside, headings


def parse_units(text):
    lines, outside, headings = _scan(text)
    units, ancestors = [], []
    for i, (start, heading_end, level, title) in enumerate(headings):
        while ancestors and ancestors[-1][2] >= level:
            ancestors.pop()
        parents = list(ancestors)
        next_start = headings[i+1][0] if i+1 < len(headings) else len(text)
        # The introduction of an ancestor is never removed; duplicate it as context.
        intro = text[start:next_start]
        ancestors.append((start, heading_end, level, title, intro))
        if i+1 < len(headings) and headings[i+1][2] > level:
            continue
        body = text[start:next_start]
        context = ''.join(p[4] for p in parents)
        body_lines, body_outside, _ = _scan(body)
        conditions = tuple(body_lines[j].strip() for j in sorted(body_outside)
                           if CONDITION.match(body_lines[j]) and SCOPE.search(body_lines[j]))
        protected = level == 1 or bool(CORE_TITLE.fullmatch(title.strip()) or GLOBAL.search(body)
                                        or any(CORE_TITLE.fullmatch(p[3].strip()) for p in parents))
        ident = sha(str(start) + ':' + body)[:24]
        units.append(Unit(ident, start, next_start, heading_end,
                          tuple(p[3] for p in parents) + (title,), sha(body), body,
                          context, protected, conditions))
    return units


PROTECTED_TITLE = re.compile(r'(?i)^(?:protected core|受保护核心)$')
# Fixed tools and statements about the Skill's own scope belong to every task the Skill serves.
FIXED_TOOL = re.compile(r'(?<![\w.-])(?:/opt/rsi-skills/)?(?:scripts|tests)/[\w.-]+')
SKILL_SCOPE = re.compile(r'(?i)\bthis skill\b|本技能|该技能')
NAVIGATION = re.compile(r' {0,3}(?:(?:[-*+]|\d{1,3}[A-Za-z]?[.)])[ \t]+)?Specific guidance \([^\n]*\): '
                        r'\[[^\]\n]+\]\(references/[^)\s]+\)\n*')
ITEM = re.compile(r'^( {0,3})(?:[-*+]|\d{1,3}[A-Za-z]?[.)])[ \t]+\S')


@dataclass(frozen=True)
class ExperienceUnit:
    """One top-level list item or paragraph of a heading's own body.

    Nested lists, continuation lines, fences and indented code stay inside the
    block that introduces them. `fingerprint` ignores trailing blank lines so a
    unit keeps its identity when neighbours move.
    """
    id: str
    kind: str
    start: int
    end: int
    heading_path: tuple
    content_sha256: str
    fingerprint: str
    text: str
    protected: bool

    def record(self):
        return asdict(self)


def parse_experience_units(text):
    lines, outside, headings = _scan(text)
    positions, offset = [], 0
    for line in lines:
        positions.append(offset)
        offset += len(line)
    line_at = {p: i for i, p in enumerate(positions)}
    units, stack = [], []
    for n, (start, heading_end, level, title) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = tuple(t for _, t in stack)
        body_end = headings[n + 1][0] if n + 1 < len(headings) else len(text)
        first = line_at.get(heading_end, len(lines))
        blocks, current, blank = [], None, False
        for i in range(first, len(lines)):
            if positions[i] >= body_end:
                break
            line = lines[i].rstrip('\n')
            if i not in outside:
                if current is None:
                    current = {'kind': 'paragraph', 'indent': 0, 'first': i, 'last': i}
                    blocks.append(current)
                current['last'] = i
                blank = False
                continue
            if not line.strip():
                if current is not None:
                    current['last'] = i
                blank = True
                continue
            item = ITEM.match(line)
            indent = len(line) - len(line.lstrip(' '))
            if current is None:
                starts = True
            elif item and (current['kind'] == 'paragraph' or indent <= current['indent']):
                starts = True
            elif blank:
                starts = not (current['kind'] == 'item' and indent >= 2)
            else:
                starts = False
            if starts:
                current = {'kind': 'item' if item else 'paragraph', 'indent': indent if item else 0,
                           'first': i, 'last': i}
                blocks.append(current)
            current['last'] = i
            blank = False
        locked = any(PROTECTED_TITLE.fullmatch(t.strip()) for t in path)
        for number, block in enumerate(blocks):
            begin = positions[block['first']]
            finish = min(body_end, positions[block['last']] + len(lines[block['last']]))
            body = text[begin:finish]
            ident = sha('experience:' + str(begin) + ':' + body)[:24]
            scope = level == 1 and number == 0 and block['kind'] == 'paragraph'
            protected = (locked or scope or bool(GLOBAL.search(body)) or bool(FIXED_TOOL.search(body))
                         or bool(SKILL_SCOPE.search(body)) or bool(NAVIGATION.fullmatch(body)))
            units.append(ExperienceUnit(ident, block['kind'], begin, finish, path, sha(body),
                                        sha(body.strip()), body, protected))
    return units


def rebase_links(text, source, target, files):
    """Only simple inline Markdown links are supported; ambiguous syntax fails closed."""
    lines, outside, _ = _scan(text)
    result = []
    for i, line in enumerate(lines):
        if source == target:
            result.append(line)
            continue
        if re.search(r'(?i)\b(?:as above|as below|see above|see below|previous section|following section|aforementioned)\b|上文|下文|前文|<!--|<(?:div|pre|script|table|details|section)\b', line):
            raise UnsafeMarkdown('Ambiguous cross-section or HTML dependency')
        # Definitions, reference-style/HTML links and relative paths in commands
        # need a richer dependency parser; never guess their meaning.
        if re.search(r'\]\[|^\s*\[[^\]]+\]:|<a\s|<img\s|!\[', line):
            raise UnsafeMarkdown('Unsupported link dependency')
        if any(LINK.search(m[2]) for m in re.finditer(r'(`+)(.*?)\1', line)):
            raise UnsafeMarkdown('Markdown link example inside inline code is ambiguous')
        visible = LINK.sub('', line) if i in outside else line
        if re.search(r'(?<![/\w])(?:references|scripts|tests)/', visible):
            raise UnsafeMarkdown('Ambiguous bare relative Skill path')
        if i not in outside:
            result.append(line)
            continue
        # Code spans are never links, so `d["key"]` is not a shortcut reference.
        prose = re.sub(r'(`+)(.*?)\1', '', visible)
        if re.search(r'\[[^\]\n]+\]', prose):
            raise UnsafeMarkdown('Ambiguous shortcut link or footnote dependency')
        if '](' in prose:
            raise UnsafeMarkdown('Unsupported inline link syntax')
        def replace(match):
            raw = match[2]
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or parsed.path.startswith('/'):
                return match[0]
            dest = posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(parsed.path))) if parsed.path else source
            if dest not in files or dest.startswith('../'):
                raise UnsafeMarkdown('Missing or outside-Skill dependency')
            relative = posixpath.relpath(dest, posixpath.dirname(target) or '.')
            suffix = ('?' + parsed.query if parsed.query else '') + ('#' + parsed.fragment if parsed.fragment else '')
            return match[1] + '(' + relative + suffix + ')'
        result.append(LINK.sub(replace, line))
    return ''.join(result)


def stub(unit, source, target):
    heading = unit.text[:unit.heading_end - unit.start]
    relative = posixpath.relpath(target, posixpath.dirname(source) or '.')
    return heading + '\nSee [complete guidance](' + relative + ').\n\n'
