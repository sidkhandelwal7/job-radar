"""Query language → SQL WHERE.

    base > 110000 AND category:big_tech AND days_to_close < 14 AND NOT requires_clearance
    company:"capital one" nyc new grad
    beats_baseline:clearly_better OR (fit >= 0.7 AND NOT applied)

Rules: AND binds tighter than OR; juxtaposition is AND; bare words search title/description/company
via FTS5; a bare field name that is boolean tests truth (`NOT applied`). `field:value` is equality for
enums/numbers and case-insensitive contains for text fields. Comparison ops: = != > >= < <= ~ (contains).
Field list is exported for the UI's "All filters" help.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from lark import Lark, Token, Transformer, v_args
from lark.exceptions import VisitError

GRAMMAR = r"""
?start: expr
?expr: expr OR term          -> or_
     | term
?term: term AND factor       -> and_
     | term factor           -> and_
     | factor
?factor: NOT factor          -> not_
       | "(" expr ")"
       | comparison
       | word
comparison: FIELD OP value   -> cmp
          | FIELD ":" value  -> colon
value: DATE                  -> datev
     | NUM                   -> number
     | ESCAPED_STRING        -> string
     | BARE                  -> bare
word: ESCAPED_STRING         -> text
    | BARE                   -> text

OR.4: /or\b/i
AND.4: /and\b/i
NOT.4: /not\b/i | "-"
DATE.3: /\d{4}-\d{2}-\d{2}/
NUM.2: /\d+(\.\d+)?[kK]?(?![\w.])/
OP: ">=" | "<=" | "!=" | "=" | ">" | "<" | "~"
FIELD.1: /[a-zA-Z_][a-zA-Z0-9_]*(?=\s*(:|>=|<=|!=|=|>|<|~))/
BARE: /[^\s()":=<>~\-][^\s()":]*/
%import common.ESCAPED_STRING
%import common.WS
%ignore WS
"""


@dataclass(frozen=True)
class Field:
    name: str
    sql: str  # SQL expression over `p` (postings alias)
    kind: str  # number | text | enum | bool | date | json_list
    group: str
    help: str = ""
    aliases: tuple[str, ...] = ()


# Single source of truth for facet names; the UI renders "All filters" from this table.
FIELDS: list[Field] = [
    # --- Verdict / value
    Field(
        "beats_baseline",
        "p.beats_baseline",
        "enum",
        "Value",
        "clearly_better | arguably_better | worse",
        ("verdict",),
    ),
    Field("composite", "p.composite_score", "number", "Value", "0–1 composite score", ("score",)),
    Field("priority", "p.priority", "number", "Value", "queue priority"),
    Field("rank", "p.apply_priority_rank", "number", "Value", "apply-first rank (1 = top)"),
    Field("ev", "p.ev_estimate", "number", "Value", "expected value, $"),
    Field("urgency", "p.urgency_score", "number", "Value"),
    Field("comp_score", "p.comp_score", "number", "Value"),
    Field(
        "career_capital", "p.career_capital_score", "number", "Value", "", ("career_capital_score",)
    ),
    Field("fit", "p.fit_score", "number", "Value", "resume fit 0–1", ("fit_score",)),
    Field("winnability", "p.winnability_score", "number", "Value", "", ("winnability_score",)),
    Field("location_score", "p.location_score", "number", "Value"),
    Field("culture_score", "p.culture_score", "number", "Value"),
    Field("floor", "p.floor_result", "enum", "Value", "pass | fail | exempt"),
    Field("in_scope", "COALESCE(p.in_scope, 1)", "bool", "Value", "passes the scope rules"),
    Field(
        "queue_action",
        "p.queue_action",
        "enum",
        "Value",
        "apply_today | apply_this_week | watch | get_referral_first | blocked_needs_prep",
    ),
    # --- Comp
    Field(
        "base",
        "COALESCE(p.base_est, p.base_posted_max, p.base_posted_min)",
        "number",
        "Comp",
        "best available base $ (posted or estimated)",
    ),
    Field("base_min", "p.base_posted_min", "number", "Comp", "posted range low"),
    Field("base_max", "p.base_posted_max", "number", "Comp", "posted range high"),
    Field("base_est", "p.base_est", "number", "Comp"),
    Field(
        "has_posted_comp",
        "(p.base_posted_min IS NOT NULL OR p.base_posted_max IS NOT NULL)",
        "bool",
        "Comp",
        "",
        ("posted_comp",),
    ),
    Field("comp_source", "p.comp_source", "enum", "Comp"),
    Field("comp_confidence", "p.comp_confidence", "number", "Comp"),
    Field("tc", "p.tc_year1_est", "number", "Comp", "year-1 TC estimate"),
    Field("signing", "p.signing_est", "number", "Comp"),
    Field("equity_type", "p.equity_type", "enum", "Comp"),
    Field(
        "effective_value",
        "p.effective_value",
        "number",
        "Comp",
        "COL-adjusted + location premium − tax delta",
    ),
    Field(
        "real_terms",
        "p.real_terms_vs_baseline",
        "number",
        "Comp",
        "pure COL/tax delta vs the baseline (informational)",
    ),
    Field("after_tax", "p.base_after_tax_est", "number", "Comp"),
    Field(
        "tax_rate",
        "p.tax_rate",
        "number",
        "Comp",
        "state+local effective rate at the job's location (0 = no income tax)",
    ),
    Field(
        "tax_delta",
        "p.tax_delta_vs_baseline",
        "number",
        "Comp",
        "$ more (+) or less (−) state/local tax than VA",
    ),
    Field("premium", "p.location_utility_premium", "number", "Comp", "location premium applied"),
    Field("p_offer", "p.p_offer", "number", "Fit"),
    Field("prep_hours", "p.prep_hours_est", "number", "Fit"),
    Field(
        "prep",
        "p.prep_archetype",
        "enum",
        "Fit",
        "leetcode_grind | system_design | take_home | domain_finance | behavioral_heavy",
        ("prep_archetype",),
    ),
    # --- Role
    Field("title", "p.title", "text", "Role"),
    Field(
        "family",
        "p.role_family",
        "enum",
        "Role",
        "software_engineering | ml_ai | data_engineering | devops_sre | security | quant | …",
        ("role_family",),
    ),
    Field("subfamily", "p.role_subfamily", "enum", "Role", "", ("role_subfamily",)),
    Field(
        "seniority",
        "p.seniority",
        "enum",
        "Role",
        "new_grad | mid | senior | staff | principal | manager | executive | internship | unknown",
    ),
    Field(
        "new_grad",
        "p.is_new_grad",
        "bool",
        "Role",
        "title/description signals new-grad",
        ("is_new_grad",),
    ),
    Field("stretch", "p.is_stretch", "bool", "Role", "1–2 YoE stretch req"),
    Field(
        "program",
        "p.program_type",
        "enum",
        "Role",
        "rotational | analyst_program | new_grad_program | apprenticeship | standard",
        ("program_type",),
    ),
    Field("min_years", "p.min_years_experience", "number", "Role"),
    Field(
        "employment",
        "p.employment_type",
        "enum",
        "Role",
        "full_time | internship | contract | part_time | unknown",
        ("employment_type",),
    ),
    Field("tag", "p.tech_tags_json", "json_list", "Role", "tech tag, e.g. tag:dotnet", ("tech",)),
    Field(
        "requires_clearance",
        "COALESCE(p.requires_clearance, 0)",
        "bool",
        "Role",
        "",
        ("clearance",),
    ),
    Field(
        "requires_advanced_degree",
        "COALESCE(p.requires_advanced_degree, 0)",
        "bool",
        "Role",
        "",
        ("advanced_degree",),
    ),
    Field("sponsorship", "p.sponsorship", "enum", "Role", "offers | does_not_offer | unknown"),
    # --- Employer
    Field("company", "p.company_name", "text", "Employer"),
    Field(
        "category",
        "p.target_category",
        "enum",
        "Employer",
        "big_tech_swe | bank_and_exchange_tech | fintech_infrastructure | elite_infra_startup | defense_and_gov_tech | quant_dev_research_trading | ai_lab",
        ("target_category",),
    ),
    Field("tier", "p.company_tier", "number", "Employer", "1 | 2 | 3"),
    Field(
        "dream",
        "p.is_dream_list",
        "bool",
        "Employer",
        "dream-list company",
        ("dream_list", "is_dream_list"),
    ),
    Field(
        "same_market",
        "p.same_market_as_baseline_offer",
        "bool",
        "Employer",
        "DC-metro financial services (Capital One corridor)",
        ("same_market_as_baseline_offer",),
    ),
    Field(
        "referral",
        "p.referral_likelihood",
        "enum",
        "Employer",
        "likely | possible | unknown",
        ("referral_likelihood",),
    ),
    Field("referral_secured", "p.referral_secured", "bool", "Employer"),
    # --- Location
    Field(
        "metro",
        "p.primary_metro",
        "enum",
        "Location",
        "new_york | san_francisco | seattle | washington_dc | … | remote",
        ("location", "primary_metro"),
    ),
    Field("metros", "p.metros_json", "json_list", "Location", "any office metro"),
    Field("work_mode", "p.work_mode", "enum", "Location", "onsite | hybrid | remote | unknown"),
    Field("remote", "p.remote_eligible", "bool", "Location", "", ("remote_eligible",)),
    Field(
        "international",
        "p.is_international_only",
        "bool",
        "Location",
        "no US office",
        ("is_international_only",),
    ),
    Field("multiple_locations", "p.is_multiple_locations", "bool", "Location"),
    Field("country", "p.country_codes_json", "json_list", "Location", "ISO country code"),
    # --- Timing
    Field("posted", "p.posted_at", "date", "Timing", "posted date (YYYY-MM-DD)", ("posted_at",)),
    Field("first_seen", "p.first_seen_at", "date", "Timing", "", ("first_seen_at",)),
    Field("last_seen", "p.last_seen_at", "date", "Timing"),
    Field(
        "days_open",
        "CAST(julianday('now') - julianday(COALESCE(p.posted_at, p.first_seen_at)) AS INTEGER)",
        "number",
        "Timing",
    ),
    Field(
        "days_to_close",
        "CASE WHEN p.application_deadline IS NOT NULL THEN CAST(julianday(p.application_deadline) - julianday('now') AS INTEGER) ELSE NULL END",
        "number",
        "Timing",
        "days until stated deadline (null if none)",
    ),
    Field("deadline", "p.application_deadline", "date", "Timing"),
    Field(
        "est_days_to_close",
        "p.est_days_to_close",
        "number",
        "Timing",
        "learned median time-to-close minus days open",
    ),
    Field(
        "first_drop",
        "p.first_drop",
        "bool",
        "Timing",
        "first new-grad req of the season at a Tier-1/dream company",
    ),
    Field("delisted", "(p.delisted_at IS NOT NULL)", "bool", "Timing", "removed at source"),
    Field("repost", "(p.repost_of_id IS NOT NULL)", "bool", "Timing"),
    Field("changed", "p.changed_since_first_seen", "bool", "Timing"),
    # --- Workflow
    Field(
        "status",
        "p.status",
        "enum",
        "Workflow",
        "new | shortlisted | applied | dismissed | snoozed",
    ),
    Field(
        "applied",
        "EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id)",
        "bool",
        "Workflow",
        "has an application record",
    ),
    Field("starred", "p.starred", "bool", "Workflow"),
    Field(
        "snoozed",
        "(p.snooze_until IS NOT NULL AND p.snooze_until > strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        "bool",
        "Workflow",
    ),
    Field("dismiss_reason", "p.dismiss_reason", "text", "Workflow"),
    Field("user_tag", "p.tags_user_json", "json_list", "Workflow", "your own tags"),
    Field("note", "p.notes_md", "text", "Workflow"),
    # --- Meta
    Field("source", "p.source", "enum", "Meta", "company_direct | aggregator | third_party"),
    Field(
        "provider",
        "p.source_provider",
        "enum",
        "Meta",
        "greenhouse | workday | oracle | lever | ashby | … | github",
        ("source_provider",),
    ),
    Field("url_status", "p.url_status", "enum", "Meta", "live | redirected | dead | unverified"),
    Field("has_description", "p.description_fetched", "bool", "Meta"),
    Field(
        "has_requirements", "p.has_requirements", "bool", "Meta", "LLM requirement extraction done"
    ),
    Field("parse_confidence", "p.parse_confidence", "number", "Meta"),
    Field("cluster", "p.cluster_id", "number", "Meta"),
    Field("id", "p.id", "number", "Meta"),
]

_BY_NAME: dict[str, Field] = {}
for f in FIELDS:
    _BY_NAME[f.name] = f
    for a in f.aliases:
        _BY_NAME[a] = f

# Enum shorthands so `category:big_tech` works
ENUM_SHORTHANDS: dict[str, dict[str, str]] = {
    "category": {
        "big_tech": "big_tech_swe",
        "bigtech": "big_tech_swe",
        "banks": "bank_and_exchange_tech",
        "bank": "bank_and_exchange_tech",
        "exchange": "bank_and_exchange_tech",
        "fintech": "fintech_infrastructure",
        "infra": "elite_infra_startup",
        "startup": "elite_infra_startup",
        "defense": "defense_and_gov_tech",
        "gov": "defense_and_gov_tech",
        "quant": "quant_dev_research_trading",
        "ai": "ai_lab",
    },
    "metro": {
        "nyc": "new_york",
        "ny": "new_york",
        "sf": "san_francisco",
        "bay_area": "san_francisco",
        "sea": "seattle",
        "dc": "washington_dc",
        "pgh": "pittsburgh",
    },
    "beats_baseline": {
        "clearly": "clearly_better",
        "arguably": "arguably_better",
        "better": "clearly_better",
    },
    "seniority": {"newgrad": "new_grad", "ng": "new_grad", "intern": "internship"},
    "family": {
        "swe": "software_engineering",
        "software": "software_engineering",
        "ml": "ml_ai",
        "ai": "ml_ai",
        "data": "data_engineering",
        "sre": "devops_sre",
        "devops": "devops_sre",
    },
}


class QueryError(ValueError):
    pass


@dataclass
class Compiled:
    where: str
    params: list[Any]
    uses_fts: bool = False


@v_args(inline=True)
class _ToSQL(Transformer):
    def __init__(self) -> None:
        super().__init__()
        self.params: list[Any] = []
        self.uses_fts = False

    # values
    def number(self, tok: Token) -> tuple[str, Any]:
        s = str(tok)
        mult = 1
        if s[-1] in "kK":
            mult, s = 1000, s[:-1]
        v = float(s) if "." in s else int(s)
        return ("num", v * mult)

    def string(self, tok: Token) -> tuple[str, Any]:
        return ("str", str(tok)[1:-1].replace('\\"', '"'))

    def datev(self, tok: Token) -> tuple[str, Any]:
        return ("date", str(tok))

    def bare(self, tok: Token) -> tuple[str, Any]:
        s = str(tok)
        if re.fullmatch(r"-?\d+(\.\d+)?[kK]", s):
            return ("num", float(s[:-1]) * 1000)
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return ("num", float(s) if "." in s else int(s))
        if s.lower() in ("true", "yes", "on"):
            return ("bool", 1)
        if s.lower() in ("false", "no", "off"):
            return ("bool", 0)
        if s.lower() in ("null", "none"):
            return ("null", None)
        return ("str", s)

    # comparisons
    def cmp(self, field: Token, op: Token, value: tuple[str, Any]) -> str:
        return self._compare(str(field), str(op), value)

    def colon(self, field: Token, value: tuple[str, Any]) -> str:
        return self._compare(str(field), ":", value)

    def _compare(self, name: str, op: str, value: tuple[str, Any]) -> str:
        f = _BY_NAME.get(name.lower())
        if f is None:
            raise QueryError(
                f"unknown field {name!r}. Known: {', '.join(sorted(x.name for x in FIELDS))}"
            )
        vkind, v = value
        if vkind == "str" and f.name in ENUM_SHORTHANDS:
            v = ENUM_SHORTHANDS[f.name].get(str(v).lower(), v)
        if vkind == "null":
            if op in (":", "="):
                return f"{f.sql} IS NULL"
            if op == "!=":
                return f"{f.sql} IS NOT NULL"
            raise QueryError(f"cannot use {op} with null")
        if f.kind == "bool":
            truthy = (
                1
                if (vkind == "bool" and v)
                or (vkind == "num" and v)
                or (vkind == "str" and str(v).lower() in ("true", "yes", "1"))
                else 0
            )
            if op in (":", "="):
                return f"{f.sql} = {truthy}"
            if op == "!=":
                return f"{f.sql} != {truthy}"
            raise QueryError(f"cannot use {op} with boolean field {f.name}")
        if f.kind == "json_list":
            self.params.append(f'%"{str(v).lower()}"%')
            neg = "NOT " if op == "!=" else ""
            return f"{neg}LOWER(COALESCE({f.sql}, '[]')) LIKE ?"
        if f.kind == "text":
            if op in (":", "~"):
                self.params.append(f"%{v}%")
                return f"{f.sql} LIKE ? COLLATE NOCASE"
            if op == "=":
                self.params.append(str(v))
                return f"{f.sql} = ? COLLATE NOCASE"
            if op == "!=":
                self.params.append(f"%{v}%")
                return f"COALESCE({f.sql}, '') NOT LIKE ? COLLATE NOCASE"
            raise QueryError(f"cannot use {op} with text field {f.name}")
        if f.kind == "enum":
            if op in (":", "="):
                self.params.append(str(v).lower())
                return f"LOWER({f.sql}) = ?"
            if op == "!=":
                self.params.append(str(v).lower())
                return f"COALESCE(LOWER({f.sql}), '') != ?"
            if op == "~":
                self.params.append(f"%{str(v).lower()}%")
                return f"LOWER({f.sql}) LIKE ?"
            raise QueryError(f"cannot use {op} with enum field {f.name}")
        if f.kind == "date":
            sqlop = "=" if op == ":" else op
            if sqlop == "~":
                raise QueryError("~ not valid for dates")
            self.params.append(str(v))
            return f"substr({f.sql}, 1, 10) {sqlop} ?"
        # number
        sqlop = "=" if op == ":" else op
        if sqlop == "~":
            raise QueryError("~ not valid for numbers")
        if vkind not in ("num",):
            raise QueryError(f"{f.name} expects a number, got {v!r}")
        self.params.append(v)
        return f"{f.sql} {sqlop} ?"

    def text(self, tok: Token) -> str:
        s = str(tok)
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        f = _BY_NAME.get(s.lower())
        if f is not None and f.kind == "bool":
            return f"{f.sql} = 1"
        # free text → FTS5 over title/description/company
        self.uses_fts = True
        q = " ".join(f'"{w}"' for w in re.findall(r"[\w+#.]+", s)) or '""'
        self.params.append(q)
        return "p.id IN (SELECT rowid FROM postings_fts WHERE postings_fts MATCH ?)"

    def and_(self, a: str, _tok: Token | str = None, b: str | None = None) -> str:  # type: ignore[assignment]
        if b is None:  # juxtaposition: (a, b)
            b = _tok  # type: ignore[assignment]
        return f"({a} AND {b})"

    def or_(self, a: str, _tok: Token, b: str) -> str:
        return f"({a} OR {b})"

    def not_(self, _tok: Token, a: str) -> str:
        return f"(NOT {a})"


_PARSER = Lark(GRAMMAR, parser="lalr", start="start")


def compile_query(text: str | None) -> Compiled:
    text = (text or "").strip()
    if not text:
        return Compiled(where="1=1", params=[])
    try:
        tree = _PARSER.parse(text)
    except Exception as e:
        raise QueryError(f"could not parse query: {e}") from e
    t = _ToSQL()
    try:
        where = t.transform(tree)
    except VisitError as e:  # unwrap our own QueryError raised inside the transformer
        if isinstance(e.orig_exc, QueryError):
            raise e.orig_exc from None
        raise QueryError(str(e)) from e
    return Compiled(where=str(where), params=t.params, uses_fts=t.uses_fts)


def field_help() -> list[dict[str, str]]:
    return [
        {
            "name": f.name,
            "kind": f.kind,
            "group": f.group,
            "help": f.help,
            "aliases": ", ".join(f.aliases),
        }
        for f in FIELDS
    ]
