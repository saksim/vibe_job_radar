from __future__ import annotations

import re
from dataclasses import dataclass
from .models import JobRecord, Requirement
from .utils import digest


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str
    line: int
    section: str
    vibe_section: bool

    @property
    def matching_text(self) -> str:
        # Only spans admitted by _soft_wrap_scan can contain line breaks.
        # Rejoin CJK words, retain Latin word spacing; never rewrite the quote.
        text = re.sub(r'(?<=[\u3400-\u9fff])[ \t]*\r?\n[ \t]*(?=[\u3400-\u9fff])', '', self.text)
        return re.sub(r'[ \t]*\r?\n[ \t]*', ' ', text)


_HEADINGS = (
    ('responsibilities', r'岗位职责|工作职责|工作职能|职位描述|responsibilities|(?:job|role)\s+description|what\s+you(?:\s+will|[\'’]ll)\s+do|about\s+(?:the|this)\s+role'),
    ('company', r'公司介绍|关于我们|about\s+(?:us|the\s+company|[a-z][a-z0-9 &.-]{0,40})|who\s+we\s+are|company(?:\s+(?:overview|description))?'),
    ('benefits', r'福利待遇|薪酬福利|benefits(?:\s+and\s+perks)?|perks(?:\s+and\s+benefits)?|compensation(?:\s+and\s+benefits)?|what\s+we\s+offer'),
    ('preferred', r'加分项|优先条件|nice\s+to\s+have|(?:preferred|desired|bonus)\s+(?:qualifications|skills)|bonus\s+points'),
    ('requirements', r'任职要求|任职资格|岗位要求|基本要求|招聘要求|(?:minimum\s+|basic\s+|required\s+)?qualifications|(?:job\s+)?requirements|required\s+skills|what\s+you\s+bring|you\s+(?:may\s+be\s+)?(?:a\s+)?(?:good\s+)?fit\s+if'),
    ('ai', r'AI\s*编程要求|Vibe\s*Coding要求|(?:AI[ -]coding|vibe\s+coding)\s+(?:requirements|skills)'),
)


def _heading_kind(text: str) -> str:
    heading = re.sub(r'^#{1,4}\s+', '', text).rstrip('：:。!?？ ')
    if heading.startswith('【') and heading.endswith('】'):
        heading = heading[1:-1].strip().rstrip('：: ')
    if len(heading) > 80:
        return ''
    if re.fullmatch(r'about\s+you', heading, re.I):
        return 'requirements'
    return next((kind for kind, pattern in _HEADINGS if re.fullmatch(pattern, heading, re.I)), '')


_NUMBERED_PREFIX = re.compile(r'(?:[（(]\d{1,2}[）)]|\d{1,2}[、.)）])(?!\d)[ \t]*')
_NEW_OBLIGATION = re.compile(
    r'(?:(?:但是|但|然而|不过|同时|并且|并|且)[ \t]*){0,2}'
    r'(?:必须|需要|应|须|需|禁止|不得|严禁|不能|不允许|不要求|不必|不强制|无需|不需要|'
    r'不接受|不依赖|要求|熟练|精通|掌握|熟悉|具备|能够|负责|使用|推动|参与|'
    r'建立|构建|维护|设计|完成|开发|公司|关于|福利|薪酬)')
_NEGATION_BREAKS = tuple((word[:i], word[i:])
    for word in ('不要求', '不需要', '不必', '不强制', '不允许', '不能', '不得', '禁止', '严禁', '无需')
    for i in range(1, len(word)))


def _soft_wrap_scan(text: str) -> str:
    """Same-length scan view for bounded Chinese numbered-item continuations.

    An explicit new obligation is not evidence of a layout wrap. Unnumbered,
    English-only, oversized or uncertain boundaries retain their original LF.
    """
    lines = list(re.finditer(r'[^\n]*\n|[^\n]+$', text))
    scan = list(text)

    def continuation(previous: str, following: str) -> bool:
        previous, following = previous.strip(), following.strip()
        if not previous or not re.match(r'[\u3400-\u9fff]', following):
            return False
        if re.search(r'[。！？；;.!?：:][”’"）)\]】]*$', previous):
            return False
        if re.match(r'[一二三四五六七八九十百]+[、.）)]', following):
            return False
        if _heading_kind(following) or re.match(r'[^：:]{1,80}[：:]', following):
            return False
        if _NEW_OBLIGATION.match(following):
            # A split negation (不 / 要求) is a continuation, not a new positive
            # obligation. Only the existing, finite negation vocabulary qualifies.
            return any(previous.endswith(left) and following.startswith(right) for left, right in _NEGATION_BREAKS)
        return True

    i = 0
    while i < len(lines):
        first = lines[i].group().strip()
        if not _NUMBERED_PREFIX.match(first) or not re.search(r'[\u3400-\u9fff]', first):
            i += 1
            continue
        end = i + 1
        while end < len(lines) and continuation(lines[end-1].group(), lines[end].group()):
            end += 1
        if end - i <= 8 and lines[end-1].end() - lines[i].start() <= 2000:
            for line in lines[i:end-1]:
                for position in range(line.end()-2, line.end()):
                    if position >= line.start() and text[position] in '\r\n':
                        scan[position] = ' '
        i = end
    return ''.join(scan)


def _english_directive_prefix(prefix: str, modal: str) -> bool:
    connectors = r"(?:(?:but|however|and|also)\s+){0,2}"
    if re.fullmatch(connectors + r"(?:please\s+)?", prefix, re.I):
        return True
    if modal.lower().startswith("must"):
        # A condition/question can mention a ban without imposing it.
        # A completed conditional setup before a comma does not negate the
        # following directive ("If on call, candidates must not ...").
        clause_prefix = re.split(r"[,;.!?]", prefix)[-1]
        if re.search(r"\b(?:if|whether|unless|when|whenever|assuming|supposing)\b", clause_prefix, re.I):
            return False
        # An embedded subject does not override a negated matrix directive.
        # Subject modifiers such as "without experience" do not themselves
        # negate a must-not obligation.
        directive = re.search(
            r"\b(?:require(?:s|d|ment)?|expect(?:s|ed)?|ask(?:s|ed)?|instruct(?:s|ed)?|"
            r"demand(?:s|ed)?|mandate(?:s|d)?|oblig(?:ed|atory|ation))\b", prefix, re.I)
        negated = re.search(
            r"\b(?:not|never|no|cannot|(?:do|does|did|is|are|was|were|have|has|had|ca|wo|should|would|could|must|need)n['’]t)\b",
            prefix, re.I)
        if directive and negated:
            return False
        return bool(re.search(r"\b(?:you|candidates?|applicants?)\s+(?:please\s+)?$", prefix, re.I))
    # Bare "you don't" is indicative. An explicit request supplies the
    # directive context without accepting a negated "we don't require".
    return bool(re.fullmatch(
        connectors + r"(?:you\s+please\s+|(?:we\s+)?(?:require|ask|instruct)\s+(?:that\s+)?you\s+(?:please\s+)?)",
        prefix, re.I))


def spans(text: str):
    section = ""
    vibe_section = False
    pattern = r"[^\n。！？；;]+(?:[。！？；;]|(?=\n|$))"
    # Split a comma only where a new obligation/negation begins, not arbitrary noun lists.
    contrast = re.compile(r"[，,](?=\s*(?:(?:但(?:是)?|然而|不过|同时|且|并)?\s*(?:必须|需要|应当|禁止|不得|严禁|不能|不允许|不要求|不必|不强制|无需)|(?:(?:but|however|and|also)\s+){0,2}(?:you(?:\s+will|['’]ll)?\s+)?(?:must(?:n['’]t)?\b|need\s+to\b|should\b|do\s+not\b|don['’]t\b|(?:use(?!\s+(?:of|cases?)\b)|apply(?!\s+(?:to|for)\b)|employ|adopt|integrate)\b)))", re.I)
    # "but/however" introduce a contrasting obligation without a comma.
    # After "and", require a fresh explicit obligation/negation: a bare
    # coordinated verb can still belong to the preceding prohibition.
    conjunction = re.compile(r"\s+(?=(?:(?:but|however)\s+(?:also\s+)?(?:you(?:\s+will|['’]ll)?\s+)?(?:must(?:n['’]t)?\b|need\s+to\b|should\b|do\s+not\b|don['’]t\b|(?:use(?!\s+(?:of|cases?)\b)|apply(?!\s+(?:to|for)\b)|employ|adopt|integrate)\b)|and\s+(?:(?:you\s+)?(?:must(?:n['’]t)?\b|need\s+to\b|should\b|do\s+not\b|don['’]t\b)|you(?:\s+will|['’]ll)\s+(?:use(?!\s+(?:of|cases?)\b)|apply(?!\s+(?:to|for)\b)|employ|adopt|integrate)\b)))", re.I)
    command = r"(?:use(?!\s+(?:of|cases?)\b)|apply(?!\s+(?:to|for)\b)|employ|adopt|integrate)\b"
    bare_transition = re.compile(r"(?:(?:but|however|and|also)\s+){0,2}" + command, re.I)
    implicit_transition = re.compile(
        r"(?:(?:but|however|and|also)\s+){0,2}(?:must(?:n['’]t)?\b|need\s+to\b|"
        r"should\b|do\s+not\b|don['’]t\b|" + command + r")", re.I)
    candidate_command = re.compile(r"(?:(?:but|however|and|also)\s+){0,2}(?:please\s+)?(?:(?:you(?:\s+will|[\'’]ll)?|candidates?\s+will)\s+)?(?:must(?:n[\'’]t)?\b|need\s+to\b|should\b|do\s+not\b|don[\'’]t\b|" + command + r")", re.I)
    # Only clear new English clauses. Decimal/version dots, initials and
    # abbreviations such as e.g. / U.S. are not arbitrary sentence boundaries.
    english = re.compile(r"[.!?](?=[ \t]+(?:You|We|The|Our|This|Must(?:n['’]t)?|Do(?:n['’]t)?|Use|Apply|Employ|Adopt|Integrate|Build|Write|Review|Maintain|Experience|Knowledge|Proficiency|No|Please|Candidates?|Applicants?)\b)", re.I)
    for match in re.finditer(pattern, _soft_wrap_scan(text)):
        raw_match = match.group()
        endings = [m.end() for m in english.finditer(raw_match)
                   if not re.search(r'(?:\b(?:e\.g|i\.e|vs|Dr|Mr|Ms|Prof|Inc|Ltd|No)|(?:\b[A-Z]\.)+[A-Z])\.$', raw_match[:m.end()], re.I)]
        bounds = {0, *endings, len(raw_match)}
        transitions = sorted({*(m.end() for m in contrast.finditer(raw_match)),
                              *(m.end() for m in conjunction.finditer(raw_match))})
        for boundary in transitions:
            following = raw_match[boundary:].lstrip()
            if implicit_transition.match(following):
                # A comma or "and" can continue a prohibited verb list.
                # A bare positive command needs a genuine contrast.
                if (bare_transition.match(following)
                        and not re.match(r"(?:but|however)\b", following, re.I)):
                    continue
                # A bare coordinated verb inherits the preceding subject.
                # Only a candidate command can supply implicit "you".
                previous = max(point for point in bounds if point < boundary)
                preceding = raw_match[previous:boundary].lstrip()
                prefix = re.match(r"[-*•·][ \t]*", preceding) or _NUMBERED_PREFIX.match(preceding)
                if prefix:
                    preceding = preceding[prefix.end():]
                inline_heading = re.match(r'([^：:\n]{1,80})[：:]\s*', preceding)
                if inline_heading and _heading_kind(inline_heading.group(1)):
                    preceding = preceding[inline_heading.end():]
                negative = re.search(r"\b(?:must\s+not|mustn['’]t|do\s+not|don['’]t)\b", preceding, re.I)
                candidate_context = (_english_directive_prefix(preceding[:negative.start()], negative.group())
                                     if negative else bool(candidate_command.match(preceding)))
                if not candidate_context:
                    continue
            bounds.add(boundary)
        bounds = sorted(bounds)
        for lo, hi in zip(bounds, bounds[1:]):
            raw = match.group()[lo:hi]
            left = len(raw) - len(raw.lstrip())
            prefix = re.match(r"[-*•·][ \t]*", raw[left:]) or _NUMBERED_PREFIX.match(raw[left:])
            if prefix:
                left += len(prefix.group())
            right = len(raw.rstrip())
            start, end = match.start() + lo + left, match.start() + lo + right
            quote = text[start:end]
            if not quote:
                continue
            kind = _heading_kind(quote)
            if kind:
                section = quote.rstrip('：:。!?？ ')
                vibe_section = kind == 'ai'
                continue
            inline = re.match(r'([^：:\n]{1,80})[：:]\s*', quote)
            if inline and (kind := _heading_kind(inline.group(1))):
                section = inline.group(1)
                vibe_section = kind == 'ai'
                start += inline.end()
                quote = text[start:end]
                if not quote:
                    continue
            yield Span(start, end, quote, text.count("\n", 0, start), section, vibe_section)


# Capture the subject before a coordinated finite predicate. An empty subject
# ("and is expected") refers back to the original subject; a named second
# subject cannot safely inherit its polarity. Decimal dots are not boundaries.
_ENGLISH_FINITE_CONTINUATION = re.compile(
    r"(?P<connector>[,;!?]|\.(?=\s|$)|\b(?:but|however|yet|and|while|whereas)\b)\s*"
    r"(?:(?:but|however|yet|and)\s+)?"
    r"(?P<subject>[^,;.!?]{0,100}?)\b(?:is|are|will\s+be)\s+", re.I)


def _english_predicate_priority(text: str) -> str:
    optional_exception = bool(re.search(r"\b(?:except|unless)\b", text, re.I))
    text = re.split(
        r"[,;!?]|\.(?=\s|$)|\b(?:but|however|yet|although|though|while|whereas|and|"
        r"for|to|in|on|with|by|under|because|when|where|as|unless|even\s+if)\b",
        text, maxsplit=1, flags=re.I)[0]
    cue = re.search(
        r"\b(?:not\s+optional|not|never|no|prohibited|forbidden|banned|disallowed|"
        r"discouraged|restricted|optional|recommended\s+against|recommended|encouraged|mandatory|essential|"
        r"expected|required|preferred|a\s+plus)\b", text, re.I)
    word = re.sub(r"\s+", " ", cue.group().lower()) if cue else ""
    if word in {"not", "never", "no", "prohibited", "forbidden", "banned",
                "disallowed", "discouraged", "restricted", "recommended against"}:
        return "unspecified"
    if word == "optional":
        return "unspecified" if optional_exception else "not_required"
    if word in {"recommended", "encouraged", "preferred", "a plus"}:
        return "preferred"
    if word in {"not optional", "mandatory", "essential", "required"}:
        return "required"
    return "expected" if word == "expected" else ""


def strength(text: str, section: str = "") -> str:
    # Preserve explicit Chinese prohibitions in bilingual explanations.
    if re.search(r"禁止|严禁|不得|不允许|不能(?:使用|上传|发送)", text):
        return "prohibited"
    # Ordinary adverbs do not reverse a direct ban. Nested negations and
    # qualifiers such as "only" are deliberately outside this finite list.
    ban = re.search(
        r"\b(?P<modal>must\s+not|mustn['’]t|do\s+not|don['’]t)\s+"
        r"(?:(?:ever|directly|automatically|knowingly|intentionally|deliberately|personally|actually)\s+){0,2}"
        r"(?:use|upload|send)\b", text, re.I)
    if ban:
        if text.rstrip().endswith("?"):
            return "unspecified"
        reason = re.split(r"\b(?:because|since)\b", text[ban.end():], maxsplit=1, flags=re.I)
        if len(reason) == 2 and re.search(
                r"\b(?:must(?:n['’]t)?|should|need(?:s)?\s+to|have\s+to|required|"
                r"mandatory|expected|preferred|optional|recommended|prohibited|forbidden)\b",
                reason[1], re.I):
            return "unspecified"
        # All subjectless transitions use the same context check. Neither
        # company descriptions nor indicative second-person statements can
        # acquire an imperative meaning merely by being split.
        if not _english_directive_prefix(text[:ban.start()], ban.group("modal")):
            return "unspecified"
        # Infinitive purposes stay inside the ban. Unsplit contrasts,
        # coordinated actions or a fresh finite requirement need review.
        if (re.search(r"\S\s+(?:but|however|yet|nevertheless|nonetheless|instead|rather|while|whereas|although|though|unless|except)\b", text, re.I)
                or re.search(r"(?:,\s*|\b(?:and|or|still)\s+)(?:(?:also|please|still)\s+)*(?:use|apply|employ|adopt|integrate|upload|send)\b", text[ban.end():], re.I)
                or _ENGLISH_FINITE_CONTINUATION.search(text[ban.end():])):
            return "unspecified"
        return "prohibited"
    # A nested negation can require tool use ("mustn't fail to use").
    # Keep uncertain predicates out of both accepted bans and positive anchors.
    if re.search(r"\b(?:must\s+not|mustn['’]t)\b", text, re.I):
        return "unspecified"
    if re.search(r"无需|不要求|不需要|不必|不强制|not (?:required|necessary|mandatory)|no .{0,40}experience.{0,20}required|\b(?:do not|don['’]t) (?:need|have to)\b|\bneed not\b", text, re.I):
        return "not_required"
    # Noun phrases and applications are not tool-use imperatives.
    # Explicit positive/optional predicates remain distinct from heading defaults.
    if re.match(r"(?:(?:but|however|and|also)\s+){0,2}apply\s+(?:to|for)\b", text, re.I):
        return "unspecified"
    if re.match(r"(?:(?:but|however|and|also)\s+){0,2}use\s+cases?\b", text, re.I):
        return "unspecified"
    noun_priority = ""
    if re.match(r"(?:(?:but|however|and|also)\s+){0,2}use\s+of\b", text, re.I):
        predicate = re.search(r"\b(?:is|will\s+be)\s+(.+)", text, re.I)
        predicate_text = predicate.group(1) if predicate else text
        continuations = [
            m for m in _ENGLISH_FINITE_CONTINUATION.finditer(predicate_text)
            # "while you are working" is a temporal condition, not another
            # tool's obligation. "while Codex is required" remains mixed.
            if not (m.group("connector").lower() == "while"
                    and re.match(r"(?:not\s+)?(?!being\b)[a-z]+ing\b", predicate_text[m.end():], re.I))
        ] if predicate else []
        if any(re.sub(r"\b(?:also|still)\b", "", m.group("subject"), flags=re.I).strip()
               for m in continuations):
            return "unspecified"
        starts = [0, *(m.end() for m in continuations)]
        ends = [*(m.start() for m in continuations), len(predicate_text)]
        priorities = [_english_predicate_priority(predicate_text[lo:hi])
                      for lo, hi in zip(starts, ends)]
        # Shared-subject affirmative predicates remain positive. Conflicting
        # optional/mandatory predicates cannot be summarized as one strength.
        if "unspecified" in priorities or ("not_required" in priorities
                and any(p in {"required", "expected", "preferred"} for p in priorities)):
            return "unspecified"
        if "not_required" in priorities:
            return "not_required"
        ranks = {"": 0, "preferred": 1, "expected": 2, "required": 3}
        noun_priority = max(priorities, key=ranks.__getitem__)
        if continuations and noun_priority:
            if noun_priority == "expected" and _heading_kind(section) == "requirements":
                return "required"
            return noun_priority
    if noun_priority == 'preferred' or _heading_kind(section) == 'preferred' or re.search(r"优先|加分|有更好|更佳|nice.to.have|preferred|\ba plus\b", text + section, re.I):
        return "preferred"
    if noun_priority == "required" or re.search(r"必须|必备|要求|至少|精通|熟练|具备|能够|独立|熟悉|掌握|\bmust\b|\brequired\b", text, re.I):
        return "required"
    if re.search(r"负责|使用|应用|推动|参与|建立|构建|维护|设计|完成|开发", text):
        return "expected"
    if _heading_kind(section) == 'requirements' or re.match(
            r'(?:hands-on\s+)?(?:experience|proficiency|expertise|familiarity)\s+(?:with|in|using)\b|(?:proficient|comfortable)\s+(?:with|in)\b|ability\s+to\b', text, re.I):
        return "required"
    if noun_priority == "expected" or re.match(r"(?:(?:but|however|and|also)\s+){0,2}(?:(?:you(?:\s+will|['’]ll)?|candidates?\s+will)\s+)?(?:use(?!\s+(?:of|cases?)\b)|apply(?!\s+(?:to|for)\b)|employ|adopt|integrate)\b", text, re.I):
        return "expected"
    return "unspecified"


class RuleExtractor:
    version = "rules-0.2.2"

    def __init__(self, config: dict):
        self.config = config
        self.direct_patterns = [re.compile(p, re.I) for p in config["direct_patterns"]]
        self.tool_patterns = {name: re.compile(p, re.I) for name, p in config["tools"].items()}
        self.cap_patterns = {name: [re.compile(p, re.I) for p in cap["patterns"]]
                             for name, cap in config["capabilities"].items()}

    def tools_in(self, text: str) -> list[str]:
        out = []
        for name, pattern in self.tool_patterns.items():
            matches = list(pattern.finditer(text))
            if not matches:
                continue
            if name == "Cursor":
                matches = [m for m in matches if not re.match(r"\s*\.(?:execute|fetch|close|rowcount)", text[m.end():], re.I)]
                if re.search(r"数据库游标|SQL\s*(?:数据库)?游标|database\s+cursor", text, re.I):
                    matches = []
            if name == "GitHub Copilot" and not re.search(r"github|代码|编程|编码|开发", text, re.I) and re.search(r"office|ppt|excel|word|办公", text, re.I):
                matches = []
            if matches:
                out.append(name)
        return out

    def direct(self, text: str) -> bool:
        return bool(self.tools_in(text) or any(p.search(text) for p in self.direct_patterns))

    def extract(self, job: JobRecord, roles: list[str], *, group_id: str | None = None) -> list[Requirement]:
        units = list(spans(job.text))
        anchors = [s for s in units if self.direct(s.matching_text) and strength(s.matching_text, s.section) in {"required", "expected", "preferred"}
                   and _heading_kind(s.section) not in {'benefits', 'company'}]
        group_id = group_id or "g_" + job.fingerprint[:24]
        rows = []
        for s in units:
            if _heading_kind(s.section) in {'benefits', 'company'}:
                continue
            clause = s.matching_text
            tools = self.tools_in(clause)
            is_direct = self.direct(clause)
            nearby = next((a for a in anchors if a.line == s.line and a != s and abs(a.start - s.start) <= 250), None)
            if is_direct:
                relation, score = "direct", 0.95
            elif s.vibe_section or nearby:
                relation, score = "contextual", 0.80
            elif anchors:
                relation, score = "role_related", 0.55
            else:
                continue
            capabilities = {k for k, ps in self.cap_patterns.items() if any(p.search(clause) for p in ps)}
            if is_direct:
                capabilities.add("ai_coding")
            if tools:
                capabilities.add("tool_fluency")
            if not capabilities:
                continue
            priority = strength(clause, s.section)
            mixed = bool(re.search(r"(?:无需|不要求|禁止|不得).{0,100}(?:但|同时|不过).{0,100}(?:必须|要求|熟练|需要)", clause))
            ambiguous = bool(re.search(r"不接受只会|不能只|不依赖|不能依赖|\b(?:not|mustn['’]t) (?:just|only)\b|\b(?:cannot|can't|must not|mustn['’]t) rely\b", clause, re.I))
            # Building/selling the named product is not proof of using it as
            # a coding tool. Keep the original clause for explicit review.
            product_mention = any(re.search(
                r'\b(?:build|develop|design|sell|market|maintain|support)(?:ing|s)?\s+(?:the\s+)?$',
                clause[:match.start()], re.I)
                for tool in tools for match in self.tool_patterns[tool].finditer(clause))
            for cap in sorted(capabilities):
                rid = "r_" + digest(group_id + f"|{s.start}|{s.end}|{cap}")[:24]
                review = "needs_review" if job.evidence_level == "snippet" or mixed or ambiguous or product_mention or relation != "direct" or priority == "unspecified" else "rule_accepted"
                notes = []
                if relation == "role_related":
                    notes.append("same-JD supporting capability; NOT evidence that this is a Vibe Coding requirement")
                if mixed or ambiguous:
                    notes.append("mixed/ambiguous polarity; human review required")
                if priority == "unspecified":
                    notes.append("tool/topic mention without a clear candidate obligation; human review required")
                if product_mention:
                    notes.append("named product work is not proof of coding-tool use; human review required")
                if job.evidence_level == "snippet":
                    notes.append("search snippet; excluded from full-text statistics even if approved")
                rows.append(Requirement(
                    requirement_id=rid, record_id=job.record_id, job_group_id=group_id, capability=cap,
                    quote=s.text, start=s.start, end=s.end, relation=relation, strength=priority,
                    tools=tools, rule_score=score, extraction_method=self.version, review_status=review,
                    roles=roles, evidence_level=job.evidence_level, is_synthetic=job.is_synthetic,
                    platform=job.platform, title=job.title, company=job.company, url=job.url,
                    source_record_ids=[job.record_id], source_urls=[job.url] if job.url else [],
                    context_quote=nearby.text if nearby else (s.section if s.vibe_section else ""), notes="; ".join(notes)))
        return rows


def hard_constraints(job: JobRecord, group_id: str, roles: list[str]) -> list[dict]:
    patterns = {
        "education": r"本科|硕士|博士|学士|大专|学历|学位",
        "experience": r"\d+\s*[-~至到]?\s*\d*\s*年.{0,10}经验|经验.{0,10}\d+\s*年",
        "work_mode": r"驻场|到岗|坐班|远程|出差|工作地点",
        "credential": r"资格证|资格认证|职称|\bPMP\b",
    }
    rows = []
    for s in spans(job.text):
        for category, pattern in patterns.items():
            if re.search(pattern, s.matching_text, re.I):
                rows.append({"constraint_id": "h_" + digest(group_id + str(s.start) + category)[:20],
                             "job_group_id": group_id, "record_id": job.record_id, "roles": roles,
                             "category": category, "quote": s.text, "start": s.start, "end": s.end,
                             "strength": strength(s.matching_text, s.section), "url": job.url,
                             "evidence_level": job.evidence_level, "is_synthetic": job.is_synthetic,
                             "note": "Do not infer satisfaction from polished wording; verify separately."})
    return rows


def apply_reviews(requirements: list[Requirement], reviews: dict) -> list[str]:
    unknown = sorted(set(reviews) - {r.requirement_id for r in requirements})
    for r in requirements:
        item = reviews.get(r.requirement_id)
        if item is None or (isinstance(item, dict) and item.get("decision") == "pending"):
            continue
        if not isinstance(item, dict) or item.get("decision") not in {"approve", "reject"} or not item.get("reviewer") or not item.get("reason"):
            raise ValueError(f"review {r.requirement_id} needs decision, reviewer, reason")
        r.review_status = "approved" if item["decision"] == "approve" else "rejected"
        r.notes += " | human review: " + str(item["reason"])
    return unknown
