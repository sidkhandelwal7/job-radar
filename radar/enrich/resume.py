"""§8.5 resume gap analysis with the §1b edges and gaps applied explicitly.

Deterministic part (always runs):
  * tech-tag overlap weighted by your proficiency (config.profile.skills), with the §1b rules:
    edge tags are rewarded, gap tags and deep-infra requirements are penalized, the domain bonus
    applies only to the categories you name, and the "stack mismatch" gap is named every time it
    applies (config.profile.big_tech_stack_note).
  * evidence: every matched strength cites the resume bullet it came from.
  * local embedding similarity between the posting and your resume (when the ml extra is installed).
LLM part (Phase 3, budgeted): the enrichment model refines matched strengths / gaps / interview themes for the
top of the queue. Never required; the deterministic score stands alone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from radar.config import Config

BIG_TECH_STACK = {"java", "python", "cpp", "go"}  # the usual big-tech interview languages


def _skill(cfg: Config, tag: str) -> tuple[float, str]:
    """(proficiency, label) for a required tag from config.profile.skills; unknown → neutral 0.5."""
    s = cfg.profile.skills.get(tag)
    if s is None:
        return 0.5, tag
    return float(s.proficiency), s.label or tag


def _terms(cfg: Config, tag: str) -> list[str]:
    """Lowercase substrings that locate the evidence bullet for `tag` in the resume text."""
    s = cfg.profile.skills.get(tag)
    if s is not None and s.terms:
        return [t.lower() for t in s.terms]
    out = [tag.replace("_", " ").lower()]
    if s is not None and s.label:
        out.append(s.label.lower())
    return out


@dataclass
class ResumeProfile:
    sha: str
    text: str
    bullets: list[str]
    skills_line: str

    def evidence_for(self, tag: str, terms: list[str] | None = None) -> str | None:
        for term in terms or [tag.replace("_", " ").lower()]:
            for b in self.bullets:
                if term in b.lower():
                    return b[:220]
        return None


def load_resume(cfg: Config) -> ResumeProfile | None:
    path = cfg.resume_path
    if not path.exists():
        return None
    sha = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    cache = cfg.data_dir / "cache" / f"resume_{sha}.json"
    if cache.exists():
        d = json.loads(cache.read_text())
        return ResumeProfile(**d)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    else:  # plain text / markdown resume (examples/sample_resume.md ships one)
        text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets: list[str] = []
    cur = ""
    for ln in lines:
        if ln.startswith(("●", "•", "-", "–")):
            if cur:
                bullets.append(cur)
            cur = ln.lstrip("●•-– ").strip()
        elif cur and (ln[0].islower() or ln[0].isdigit() or ln.startswith(("and", "for", "to"))):
            cur += " " + ln
        else:
            if cur:
                bullets.append(cur)
                cur = ""
    if cur:
        bullets.append(cur)
    skills = next((ln for ln in lines if ln.lower().startswith(("programming", "skills"))), "")
    prof = ResumeProfile(sha=sha, text=text, bullets=bullets, skills_line=skills)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(asdict(prof)))
    return prof


@dataclass
class FitResult:
    fit_score: float
    stack_score: float
    embedding_sim: float | None
    domain_bonus: float
    matched_strengths: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    prep_archetype: str = "leetcode_grind"
    prep_hours_est: float = 20
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _required_tags(p: dict[str, Any]) -> list[str]:
    tags = p.get("tech_tags") or json.loads(p.get("tech_tags_json") or "[]")
    req = (p.get("requirements") or {}) if isinstance(p.get("requirements"), dict) else {}
    for lang in (req.get("languages") or []) + (req.get("frameworks") or []):
        t = _tag_for(lang)
        if t and t not in tags:
            tags.append(t)
    return tags


def _tag_for(name: str) -> str | None:
    n = name.lower().strip()
    table = {
        "c#": "csharp",
        ".net": "dotnet",
        "dotnet": "dotnet",
        "c++": "cpp",
        "golang": "go",
        "go": "go",
        "rust": "rust",
        "python": "python",
        "java": "java",
        "javascript": "javascript",
        "typescript": "typescript",
        "react": "react",
        "sql": "sql",
        "kubernetes": "kubernetes",
        "k8s": "kubernetes",
        "docker": "docker",
        "aws": "aws",
        "azure": "azure",
        "gcp": "gcp",
        "scala": "scala",
        "kotlin": "swift_kotlin",
        "swift": "swift_kotlin",
        "ruby": "ruby",
        "php": "php",
        "terraform": "terraform",
        "spark": "spark",
        "kafka": "spark",
        "angular": "angular",
        "linux": "linux",
        "c": "c",
    }
    return table.get(n)


def fit_for_posting(
    p: dict[str, Any],
    resume: ResumeProfile | None,
    cfg: Config,
    *,
    embedding_sim: float | None = None,
) -> FitResult:
    tags = _required_tags(p)
    cat = p.get("target_category")
    fam = p.get("role_family")
    sub = p.get("role_subfamily")
    matched: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    total_w = 0.0
    got = 0.0
    edge_tags = set(cfg.profile.edge_tags)
    gap_tags = set(cfg.profile.gap_tags)
    for t in tags:
        prof, label = _skill(cfg, t)
        w = 1.6 if t in edge_tags else (1.4 if t in gap_tags else 1.0)
        total_w += w
        got += w * prof
        if prof >= 0.6:
            ev = resume.evidence_for(t, _terms(cfg, t)) if resume else None
            matched.append(
                {"strength": label, "proficiency": prof, "evidence": ev, "edge": t in edge_tags}
            )
        elif prof <= 0.3:
            gaps.append(
                {
                    "gap": label,
                    "severity": "high" if t in gap_tags and prof == 0.0 else "medium",
                    "note": "absent from your stack" if prof == 0.0 else "light exposure only",
                }
            )
    stack = got / total_w if total_w else 0.55  # no tags → neutral-ish
    # §1b: always name the stack-mismatch gap when it applies (config.profile.big_tech_stack_note)
    if (
        cat == "big_tech_swe"
        and cfg.profile.big_tech_stack_note
        and not (set(tags) & edge_tags)
        and (set(tags) & BIG_TECH_STACK or not tags)
    ):
        gaps.append(
            {
                "gap": "Big Tech stack mismatch",
                "severity": "medium",
                "note": cfg.profile.big_tech_stack_note,
            }
        )
    if (
        sub in ("low_latency_systems", "distributed_infra") or fam == "devops_sre"
    ) and "distributed_systems" in gap_tags:
        gaps.append(
            {
                "gap": "deep infra / distributed systems",
                "severity": "high" if sub == "low_latency_systems" else "medium",
                "note": "large-scale distributed-systems depth is a stated gap (§1b)",
            }
        )
        stack *= 0.8
    # domain bonus only for the categories the profile names (§1c)
    domain_bonus = 0.0
    dtag = cfg.profile.domain_tag
    if dtag and cat in cfg.profile.domain_categories:
        domain_bonus = 0.12 if dtag in tags else 0.06
        dprof, dlabel = _skill(cfg, dtag)
        matched.append(
            {
                "strength": dlabel,
                "proficiency": dprof,
                "evidence": resume.evidence_for(dtag, _terms(cfg, dtag)) if resume else None,
                "edge": True,
            }
        )
    for t in cfg.profile.edge_tags:
        if t in tags:
            eprof, elabel = _skill(cfg, t)
            matched.insert(
                0,
                {
                    "strength": f"{elabel} depth (edge)",
                    "proficiency": eprof,
                    "evidence": resume.evidence_for(t, _terms(cfg, t)) if resume else None,
                    "edge": True,
                },
            )
    # combine: stack 0.5, embedding 0.3 (rescaled from typical 0.2–0.6 cosine), domain 0.2
    if embedding_sim is not None:
        emb = max(0.0, min(1.0, (embedding_sim - 0.15) / 0.45))
        fit = 0.5 * stack + 0.3 * emb + 0.2 * min(1.0, 0.5 + domain_bonus * 2.5)
    else:
        fit = 0.7 * stack + 0.3 * min(1.0, 0.5 + domain_bonus * 2.5)
    fit = max(0.0, min(1.0, fit))
    archetype, hours = _prep(p, cat, fam, sub)
    expl = (
        f"stack {stack:.2f} over {len(tags)} tags"
        + (f"; embedding sim {embedding_sim:.2f}" if embedding_sim is not None else "")
        + (f"; domain bonus +{domain_bonus:.2f}" if domain_bonus else "")
    )
    # de-dup matched by strength
    seen: set[str] = set()
    matched = [m for m in matched if not (m["strength"] in seen or seen.add(m["strength"]))]
    return FitResult(
        fit_score=round(fit, 3),
        stack_score=round(stack, 3),
        embedding_sim=(round(embedding_sim, 3) if embedding_sim is not None else None),
        domain_bonus=domain_bonus,
        matched_strengths=matched[:6],
        gaps=gaps[:6],
        prep_archetype=archetype,
        prep_hours_est=hours,
        explanation=expl,
    )


def _prep(
    p: dict[str, Any], cat: str | None, fam: str | None, sub: str | None
) -> tuple[str, float]:
    """§8.7 interview-loop archetype + prep hours (priors; learned later from your own loops)."""
    if cat == "quant_dev_research_trading":
        return "leetcode_grind", 40
    if cat == "big_tech_swe":
        return "leetcode_grind", 30
    if cat in ("bank_and_exchange_tech",):
        return "domain_finance", 12
    if cat == "fintech_infrastructure":
        return "system_design", 18
    if cat == "elite_infra_startup":
        return "take_home", 15
    if cat == "defense_and_gov_tech":
        return "behavioral_heavy", 10
    if cat == "ai_lab":
        return "system_design", 25
    return "behavioral_heavy", 10


def resume_embedding_text(resume: ResumeProfile) -> str:
    return (resume.skills_line + "\n" + "\n".join(resume.bullets))[:3000]


def resume_cache_path(cfg: Config) -> Path:
    return cfg.data_dir / "cache"
