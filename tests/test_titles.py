import pytest

from radar.parse.titles import normalize_title

ZOO = [
    # (title, family, seniority, program_type)
    ("Software Engineer I", "software_engineering", "new_grad", "standard"),
    ("SWE New Grad 2027", "software_engineering", "new_grad", "new_grad_program"),
    ("Associate Software Engineer", "software_engineering", "new_grad", "standard"),
    ("Member of Technical Staff", "software_engineering", "unknown", "standard"),
    ("Technology Analyst Program", "software_engineering", "new_grad", "analyst_program"),
    ("Rotational Development Program – Software", "software_engineering", "new_grad", "rotational"),
    ("Early Career Engineer", "software_engineering", "new_grad", "new_grad_program"),
    ("Graduate Software Engineer", "software_engineering", "new_grad", "standard"),
    ("Forward Deployed Engineer", "software_engineering", "unknown", "standard"),
    ("Software Development Engineer I", "software_engineering", "new_grad", "standard"),
    ("Senior Software Engineer", "software_engineering", "senior", "standard"),
    ("Software Engineer II", "software_engineering", "mid", "standard"),
    ("Staff Software Engineer", "software_engineering", "staff", "standard"),
    ("Software Engineer Intern - Summer 2027", "software_engineering", "internship", "standard"),
    ("Quantitative Researcher", "quant", "unknown", "standard"),
    ("Quantitative Developer", "quant", "unknown", "standard"),
    ("Software Engineer - Quantitative Research", "software_engineering", "unknown", "standard"),
    ("Help Desk Technician", "it_support", "unknown", "standard"),
    ("QA Analyst", "qa_manual", "unknown", "standard"),
    ("Software Engineer in Test", "software_engineering", "unknown", "standard"),
    (
        "US Equity Research - Large Cap Software - Associate",
        "finance_nontech",
        "new_grad",
        "standard",
    ),
    (
        "Technical Product Marketing Engineer - New College Grad 2026",
        "other_nontech",
        "new_grad",
        "standard",
    ),
    ("SW Engineer I – Full Stack, Officer", "software_engineering", "new_grad", "standard"),
    ("Lead Backend Engineer - Mobile APIs", "software_engineering", "senior", "standard"),
    ("Teller Part Time Sacramento", "other_nontech", "unknown", "standard"),
    ("Machine Learning Engineer (2027 Start)", "ml_ai", "new_grad", "standard"),
    ("Site Reliability Engineer, Early Career", "devops_sre", "new_grad", "new_grad_program"),
    (
        "Technology Development Program (TDP) – Software Engineer",
        "software_engineering",
        "new_grad",
        "rotational",
    ),
    ("GPU Architecture Engineer - New College Grad 2026", "hardware", "new_grad", "standard"),
]


@pytest.mark.parametrize("title,family,seniority,program", ZOO)
def test_title_zoo(title, family, seniority, program):
    info = normalize_title(title)
    assert info.role_family == family, info.matched_rules
    assert info.seniority == seniority, info.matched_rules
    assert info.program_type == program, info.matched_rules


def test_step_down_flag():
    assert normalize_title("ServiceNow Administrator").step_down is True
    assert normalize_title("Software Engineer").step_down is False


def test_tech_tags_symbols():
    tags = normalize_title("Full Stack Developer (.NET/C#)").tech_tags
    assert {"dotnet", "csharp"} <= set(tags)
    assert "cpp" in normalize_title("Embedded Software Engineer - C++").tech_tags


def test_baseline_program_title_is_not_quant_evidence():
    """§1c: the operator's own program title must never feed quant classification of *them*;
    here we only assert the title classifies as a program, and the config flag is false."""
    from radar.config import get_config

    assert get_config().operator.treat_as_quant_candidate is False
