"""`radar` command-line interface."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from radar import __version__, db
from radar.config import Config, get_config
from radar.util import ago_human, utcnow_iso

app = typer.Typer(
    help="Job Radar — personal new-grad job discovery, ranking, and application tracking.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
apps_app = typer.Typer(help="Applications tracker (§15). Permanent records.", no_args_is_help=True)
app.add_typer(apps_app, name="apps")
console = Console()
err = Console(stderr=True)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _conn(cfg: Config | None = None) -> sqlite3.Connection:
    cfg = cfg or get_config()
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    return conn


@app.callback()
def _main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging to stderr"),
) -> None:
    _setup_logging(verbose)
    try:
        from radar.secrets import load_secrets

        load_secrets()
    except PermissionError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(2) from None
    except Exception:  # config may be absent before `radar init`; secrets are optional
        pass


secret_app = typer.Typer(
    help="Secrets live in data/secrets.env (git-ignored, 0600); never echoed or logged."
)
app.add_typer(secret_app, name="secret")


@secret_app.command("set")
def secret_set(key: str = typer.Argument(..., help="e.g. TELEGRAM_BOT_TOKEN")) -> None:
    """Prompt for a value with hidden input and store it in data/secrets.env (created 0600)."""
    from radar.secrets import SECRET_KEYS, set_secret

    if key not in SECRET_KEYS:
        err.print(f"[yellow]{key} is not one of the known keys {SECRET_KEYS}; storing it anyway[/]")
    value = typer.prompt(f"{key}", hide_input=True).strip()
    if not value:
        raise typer.Exit(1)
    path = set_secret(key, value)
    import stat

    console.print(
        f"stored {key} in {path} (mode {oct(stat.S_IMODE(path.stat().st_mode))}, git-ignored: data/)"
    )


@secret_app.command("unset")
def secret_unset(key: str = typer.Argument(...)) -> None:
    from radar.secrets import unset_secret

    console.print("removed" if unset_secret(key) else "not set")


@secret_app.command("list")
def secret_list() -> None:
    """Keys only — never values."""
    from radar.secrets import list_keys, secrets_path

    console.print(f"{secrets_path()}: {', '.join(list_keys()) or '(empty)'}")


@app.command()
def version() -> None:
    """Print version."""
    console.print(f"job-radar {__version__}")


@app.command()
def init() -> None:
    """Create the database, apply migrations, and sync the company registry."""
    cfg = get_config()
    from radar.fetch.registry import sync_registry

    conn = db.connect(cfg.db_path)
    applied = db.migrate(conn)
    res = sync_registry(conn, cfg)
    console.print(
        f"[green]db[/] {cfg.db_path}  migrations applied: {applied or 'none (up to date)'}"
    )
    console.print(
        f"[green]registry[/] {res['companies']} companies, {res['sources']} sources synced from config/companies.yaml"
    )
    console.print(f"[green]raw store[/] {cfg.raw_dir}")


@app.command()
def sources(provider: str | None = typer.Option(None, help="Filter by provider")) -> None:
    """List registry sources and their fetch health."""
    conn = _conn()
    rows = db.all_rows(
        conn,
        "SELECT c.name, c.tier, c.is_dream_list, cs.* FROM company_sources cs JOIN companies c ON c.id = cs.company_id "
        + ("WHERE cs.provider = ? " if provider else "")
        + "ORDER BY c.is_dream_list DESC, c.tier, c.name",
        (provider,) if provider else (),
    )
    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    for col in (
        "company",
        "tier",
        "provider",
        "slug",
        "cadence",
        "on",
        "last fetch",
        "rows",
        "typical",
        "fail",
        "error",
    ):
        t.add_column(col)
    for r in rows:
        t.add_row(
            ("★ " if r["is_dream_list"] else "") + r["name"],
            str(r["tier"] or ""),
            r["provider"],
            r["slug"],
            r["cadence"],
            "✓" if r["enabled"] else "✗",
            ago_human(r["last_fetched_at"]),
            str(r["last_row_count"] if r["last_row_count"] is not None else ""),
            f"{r['typical_row_count']:.0f}" if r["typical_row_count"] else "",
            str(r["consecutive_failures"] or ""),
            (r["last_error"] or "")[:40],
        )
    console.print(t)


@app.command()
def fetch(
    provider: list[str] = typer.Option(
        None, "--provider", "-p", help="Only these providers (repeatable)"
    ),
    company: list[str] = typer.Option(
        None, "--company", "-c", help="Only these company slugs/names (repeatable)"
    ),
    full: bool = typer.Option(
        False, "--full", help="Force a full scan (delist-safe) on every source"
    ),
    detail: str | None = typer.Option(
        None, "--detail", help="Detail-fetch policy: all_new | title_prefilter | none"
    ),
    verify: bool = typer.Option(
        False, "--verify", help="HTTP/API-verify links of newly seen postings afterwards"
    ),
    all_sources: bool = typer.Option(
        False,
        "--all",
        help="Ignore cadences and poll every enabled source (default: only sources that are due)",
    ),
) -> None:
    """Poll due sources → store raw payloads → normalize → upsert postings → detect delists."""
    cfg = get_config()
    conn = _conn(cfg)
    from radar.fetch.pipeline import fetch_all
    from radar.fetch.registry import source_specs, sync_registry

    sync_registry(conn, cfg)
    specs = []
    for comp in company or [None]:
        specs += source_specs(
            conn,
            providers=set(provider) if provider else None,
            company=comp,
            due_only=not (all_sources or company or full),
        )
    if not specs:
        console.print("[dim]nothing due (use --all to poll everything)[/]")
        raise typer.Exit(0)
    console.print(
        f"fetching {len(specs)} source(s)… (detail policy: {detail or cfg.fetch.detail_fetch})"
    )

    def progress(o: Any) -> None:
        mark = "[green]✓[/]" if o.ok else "[red]✗[/]"
        extra = " [yellow]304[/]" if o.not_modified else ""
        drift = " [red]DRIFT[/]" if o.drift else ""
        console.print(
            f"  {mark} {o.spec.company_name:<22} {o.spec.provider:<10} {o.mode:<11} rows={o.rows:<5} new={o.new:<4} chg={o.changed:<3} "
            f"delist={o.delisted:<3} details={o.details_fetched:<4} req={o.requests:<4} {o.elapsed_ms / 1000:.1f}s{extra}{drift}"
            + (f" [red]{o.error}[/]" if o.error else "")
        )

    summary = asyncio.run(
        fetch_all(
            conn,
            cfg,
            specs,
            force_full=full,
            detail_policy=detail,
            progress=progress,
            budget_seconds=-1,  # a human-run fetch is unlimited; the scheduler's cycle is budgeted
        )
    )
    s = summary.stats
    console.print(
        f"\n[bold]run #{summary.run_id}[/]: {s['sources_ok']}/{s['sources']} sources ok · {s['rows_seen']} rows · "
        f"[green]{s['new']} new[/] · {s['changed']} changed · {s['delisted']} delisted · {s['relisted']} relisted · "
        f"{s['details_fetched']} details · {s['requests']} requests · {s['bytes'] / 1e6:.1f} MB"
        + (f" · [red]{s['drift_alarms']} drift alarm(s)[/]" if s["drift_alarms"] else "")
    )
    if summary.cluster_stats:
        cs = summary.cluster_stats
        console.print(
            f"clustering: {cs.multi_clusters} multi-source clusters · {cs.siblings_hidden} duplicate rows folded · {cs.reposts_linked} reposts linked · {cs.elapsed_s}s"
        )
    if verify:
        new_ids = [pid for o in summary.outcomes for pid in o.new_posting_ids]
        if new_ids:
            _run_verify(conn, cfg, posting_ids=new_ids[:2000])
    total = db.scalar(conn, "SELECT COUNT(*) FROM postings")
    live = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE delisted_at IS NULL")
    console.print(f"master list: {total} postings ({live} currently listed)")


# ---------------------------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------------------------


def default_view_where() -> str:
    return "p.in_default_view = 1"


def suppression_breakdown(
    conn: sqlite3.Connection, base_where: str, params: list[Any]
) -> list[tuple[str, int]]:
    """How many matching rows each default-view rule removes (first matching rule wins)."""
    from radar.score.views import LABELS

    rows = db.all_rows(
        conn,
        f"SELECT p.suppressed_reason k, COUNT(*) n FROM postings p WHERE ({base_where}) AND p.in_default_view = 0 GROUP BY k ORDER BY n DESC",
        params,
    )
    return [(LABELS.get(r["k"], r["k"] or "?"), int(r["n"])) for r in rows]


def _fmt_base(r: sqlite3.Row) -> str:
    if r["base_posted_min"] or r["base_posted_max"]:
        lo, hi = r["base_posted_min"], r["base_posted_max"]
        if lo and hi and lo != hi:
            return f"${lo / 1000:.0f}–{hi / 1000:.0f}k"
        return f"${(hi or lo) / 1000:.0f}k"
    if r["base_est"]:
        return f"[dim]~${r['base_est'] / 1000:.0f}k est[/]"
    return "[dim]—[/]"


def _fmt_loc(r: sqlite3.Row) -> str:
    try:
        locs = json.loads(r["locations_json"] or "[]")
    except json.JSONDecodeError:
        locs = []
    names = []
    for loc in locs:
        n = loc.get("metro_name") or (
            loc["raw"] if loc.get("kind") != "international" else f"{loc['raw']} 🌐"
        )
        if loc.get("kind") == "remote":
            n = "Remote" + (f" ({loc['state']})" if loc.get("state") else "")
        if n not in names:
            names.append(n)
    s = "; ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    if r["work_mode"] == "hybrid":
        s += " [dim]hybrid[/]"
    return s or "[dim]?[/]"


def _fmt_link(r: sqlite3.Row) -> str:
    st = r["url_status"]
    when = ago_human(r["url_last_verified_at"])
    method = r["url_verify_method"]
    if st == "live" and method == "source_presence":
        return f"[green]listed at source {when}[/]"
    if st in ("live", "redirected"):
        return f"[green]verified live {when}[/]"
    if st == "dead":
        return f"[red]link dead — req likely closed ({when})[/]"
    return f"[yellow]unverified ({when})[/]"


@app.command("filter")
def filter_cmd(
    query: str = typer.Argument(
        "", help="e.g. 'base > 110000 AND category:big_tech AND NOT requires_clearance'"
    ),
    limit: int = typer.Option(50, "--limit", "-n"),
    everything: bool = typer.Option(
        False, "--all", "-a", help="Master view: no default suppressions"
    ),
    sort: str = typer.Option(
        "first_seen_at DESC", "--sort", "-s", help="SQL order-by over posting columns"
    ),
    out_csv: Path | None = typer.Option(
        None, "--csv", help="Write matching rows to CSV instead of printing"
    ),
    preset: str | None = typer.Option(
        None, "--preset", help='Saved filter by name, e.g. "Clearly Better" (see `radar presets`)'
    ),
) -> None:
    """Query the master list. Default view hides out-of-scope rows and says exactly how many and why."""
    from radar.query import QueryError, compile_query

    conn = _conn()
    if preset:
        pr = db.one(conn, "SELECT * FROM saved_filters WHERE name = ? COLLATE NOCASE", (preset,))
        if not pr:
            err.print(f"[red]no saved filter named {preset!r}[/] — `radar presets` lists them")
            raise typer.Exit(2)
        query = (pr["query"] + " " + query).strip()
        sort = pr["sort"] or sort
        import yaml as _yaml

        cfgp = next(
            (
                x
                for x in (
                    _yaml.safe_load((get_config().root / "config" / "presets.yaml").read_text())
                    or {}
                ).get("presets", [])
                if x["name"].lower() == pr["name"].lower()
            ),
            {},
        )
        if cfgp.get("default_view") is False:
            everything = True
    try:
        q = compile_query(query)
    except QueryError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(2) from None
    master_total = db.scalar(conn, f"SELECT COUNT(*) FROM postings p WHERE {q.where}", q.params)
    where = q.where if everything else f"({q.where}) AND {default_view_where()}"
    shown_total = db.scalar(conn, f"SELECT COUNT(*) FROM postings p WHERE {where}", q.params)
    rows = db.all_rows(
        conn,
        f"SELECT p.* FROM postings p WHERE {where} ORDER BY {sort} LIMIT ?",
        [*q.params, limit],
    )
    if out_csv:
        _write_csv(out_csv, rows)
        console.print(f"wrote {len(rows)} rows → {out_csv}")
        return
    t = Table(box=box.SIMPLE_HEAD, header_style="bold", show_lines=False)
    for col, justify in (
        ("id", "right"),
        ("company", "left"),
        ("title", "left"),
        ("location", "left"),
        ("base", "right"),
        ("seniority", "left"),
        ("seen", "left"),
        ("link", "left"),
    ):
        t.add_column(
            col, justify=justify, overflow="fold", max_width=48 if col == "title" else None
        )
    for r in rows:
        t.add_row(
            str(r["id"]),
            ("★ " if r["is_dream_list"] else "") + r["company_name"],
            r["title"] + (" [dim](no description)[/]" if not r["description_fetched"] else ""),
            _fmt_loc(r),
            _fmt_base(r),
            r["seniority"] or "",
            ago_human(r["first_seen_at"]),
            _fmt_link(r),
        )
    console.print(t)
    if everything:
        console.print(
            f"[bold]{len(rows)} of {master_total}[/] shown (master view, no suppressions)"
        )
    else:
        suppressed = master_total - shown_total
        console.print(
            f"[bold]{min(limit, shown_total)} of {shown_total}[/] shown — [dim]{suppressed} suppressed[/] from {master_total} matching. Add --all for the master view."
        )
        if suppressed:
            parts = [f"{label}: {n}" for label, n in suppression_breakdown(conn, q.where, q.params)]
            console.print("  [dim]why:[/] " + " · ".join(parts))
    console.print(
        "[dim]tip: radar show <id> · radar applied <id> · radar filter --help for the query language[/]"
    )


def _print_decomposition(r: sqlite3.Row) -> None:
    """§9e: always decompose."""
    try:
        x = json.loads(r["score_explanation_json"])
    except (TypeError, json.JSONDecodeError):
        return
    if not x.get("sub_scores"):
        console.print(f"[dim]Not scored: {x.get('scope', {}).get('reason', 'out of scope')}[/]")
        return
    console.rule("[bold]Is this better than the baseline offer?")
    loc = x.get("location")
    if loc:
        for line in loc["lines"]:
            label, amt = line["label"], line["amount"]
            is_total = label.startswith("effective")
            is_nominal = label.startswith("$")
            amount = f"${abs(amt):,.0f}"
            if not is_nominal and not is_total:
                amount = ("+" if amt >= 0 else "-") + amount
            text = f"  {amount:>12}  {label}"
            console.print(f"[bold]{text}[/bold]" if is_total else text)
        rt = loc["real_terms_vs_baseline"]
        console.print(
            f"  [dim]real-terms vs baseline (COL+tax only, informational): {'+' if rt >= 0 else '-'}${abs(rt):,.0f}[/]"
        )
    comp = x.get("comp") or {}
    console.print(f"  [dim]comp basis: {comp.get('explanation')}[/]")
    v = x["verdict"]
    console.print(
        f"  → {VERDICT_LABEL.get(v['state'], v['state'])}  [dim]({v['reason']}; confidence {v['confidence']})[/]"
    )
    console.rule("[bold]Score")
    for k, sc in x["sub_scores"].items():
        console.print(f"  {k:<22} {sc['value']:.2f} × {sc['weight']:.2f}   [dim]{sc['why']}[/]")
    console.print(
        f"  composite {x['composite']:.3f}"
        + (f"  modifiers: {', '.join(x['modifiers'])}" if x.get("modifiers") else "")
        + f"   urgency {x['urgency']['value']:.2f} (days open {x['urgency']['days_open']}, est. {x['urgency']['estimated_days_to_close']:.0f} days to close{', FIRST DROP of the season' if x['urgency'].get('first_drop') else ''})"
    )
    ev = x.get("ev") or {}
    if ev.get("p_offer") is not None:
        console.print(
            f"  EV ${r['ev_estimate']:,.0f}: P(offer) {ev['p_offer']:.0%} × (3-yr delta ${ev['three_year_effective_delta']:,.0f} + career premium ${ev['career_capital_premium']:,.0f}) − prep ${ev['prep_cost']:,.0f} − P(offer) × switching friction ${ev['switching_friction_if_offer']:,.0f}"
        )
    if x.get("same_market_as_baseline_offer"):
        console.print(
            "  [yellow]⚑ same recruiting market as the baseline offer — you decide what that means[/]"
        )
    fit = x.get("fit") or {}
    try:
        strengths = json.loads(r["matched_strengths_json"] or "[]")
        gaps = json.loads(r["gaps_json"] or "[]")
    except json.JSONDecodeError:
        strengths, gaps = [], []
    if strengths:
        console.print(
            "[bold]Matched strengths:[/] "
            + " · ".join(
                f"{m['strength']}"
                + (f" [dim]({m['evidence'][:70]}…)[/]" if m.get("evidence") else "")
                for m in strengths[:4]
            )
        )
    if gaps:
        console.print(
            "[bold]Gaps:[/] "
            + " · ".join(
                f"[{'red' if g.get('severity') == 'high' else 'yellow'}]{g['gap']}[/]"
                + (f" [dim]{g.get('note', '')[:60]}[/]" if g.get("note") else "")
                for g in gaps[:4]
            )
        )
    if fit.get("prep_archetype"):
        console.print(
            f"[bold]Loop:[/] {r['prep_archetype']} · ~{r['prep_hours_est']:.0f} prep hours · referral {r['referral_likelihood']}"
            + ("  ✓ secured" if r["referral_secured"] else "")
        )
    sc = x.get("scope") or {}
    if sc.get("floor") == "fail" or sc.get("hard_blockers"):
        console.print(
            f"[red]Suppressed:[/] {'; '.join(sc.get('floor_reasons') or sc.get('hard_blockers') or [sc.get('reason', '')])}"
        )


def _write_csv(path: Path, rows: list[sqlite3.Row]) -> None:
    cols = [
        "id",
        "company_name",
        "title",
        "primary_metro",
        "locations_json",
        "work_mode",
        "base_posted_min",
        "base_posted_max",
        "base_est",
        "seniority",
        "role_family",
        "employment_type",
        "beats_baseline",
        "composite_score",
        "apply_priority_rank",
        "status",
        "posted_at",
        "first_seen_at",
        "delisted_at",
        "url_status",
        "url_last_verified_at",
        "apply_url",
        "canonical_url",
        "source_provider",
    ]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            keys = set(r.keys())
            w.writerow([r[c] if c in keys else "" for c in cols])


@app.command()
def presets() -> None:
    """List saved filters (presets from config/presets.yaml plus your own)."""
    conn = _conn()
    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    for col in ("name", "query", "sort", "alert", "preset"):
        t.add_column(col, overflow="fold")
    for r in db.all_rows(conn, "SELECT * FROM saved_filters ORDER BY is_preset DESC, id"):
        t.add_row(
            r["name"],
            r["query"] or "(everything)",
            r["sort"] or "",
            r["alert_tier"] or "",
            "✓" if r["is_preset"] else "",
        )
    console.print(t)
    console.print(
        '[dim]use: radar filter --preset "Clearly Better"   (append extra query terms after the preset name)[/]'
    )


@app.command()
def show(posting_id: int) -> None:
    """Everything about one posting: link status, locations, comp, events."""
    conn = _conn()
    r = db.one(
        conn,
        "SELECT p.*, d.description_md, d.score_explanation_json, d.requirements_json, c.slug AS company_slug "
        "FROM postings p LEFT JOIN posting_docs d ON d.posting_id = p.id LEFT JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
        (posting_id,),
    )
    if not r:
        err.print(f"[red]posting {posting_id} not found[/]")
        raise typer.Exit(1)
    console.rule(f"[bold]#{r['id']}  {r['company_name']} — {r['title']}")
    console.print(
        f"[bold]Apply:[/] [link={r['apply_url']}]{r['apply_url']}[/link]   {_fmt_link(r)}"
    )
    if r["canonical_url"] and r["canonical_url"] != r["apply_url"]:
        console.print(f"[bold]Canonical:[/] {r['canonical_url']}")
    console.print(
        f"[bold]Location:[/] {_fmt_loc(r)}   work mode: {r['work_mode']}   metros: {r['metros_json']}"
    )
    console.print(
        f"[bold]Role:[/] {r['role_family']}/{r['role_subfamily']}  seniority={r['seniority']}  program={r['program_type']}  employment={r['employment_type']}  new_grad_signal={bool(r['is_new_grad'])}"
    )
    console.print(
        f"[bold]Base:[/] {_fmt_base(r)}  source={r['comp_source'] or '—'}   min_years={r['min_years_experience']}  clearance={r['requires_clearance']}  adv_degree={r['requires_advanced_degree']}  sponsorship={r['sponsorship']}"
    )
    if r["score_explanation_json"]:
        _print_decomposition(r)

    console.print(
        f"[bold]Timeline:[/] posted {r['posted_at'] or '?'} · first seen {r['first_seen_at']} · last seen {r['last_seen_at']}"
        + (f" · [red]delisted {r['delisted_at']}[/]" if r["delisted_at"] else "")
    )
    console.print(
        f"[bold]Source:[/] {r['source_provider']} / {r['source_slug']} / job {r['source_job_id']}   tags={r['tech_tags_json']}"
    )
    console.print(
        f"[bold]Status:[/] {r['status']}"
        + (f" ({r['dismiss_reason']})" if r["dismiss_reason"] else "")
        + (f"   notes: {r['notes_md']}" if r["notes_md"] else "")
    )
    if r["cluster_size"] and r["cluster_size"] > 1:
        sibs = db.all_rows(
            conn,
            "SELECT id, source, source_provider, title, apply_url, is_cluster_canonical, url_status FROM postings WHERE cluster_id = ? AND id != ? ORDER BY is_cluster_canonical DESC",
            (r["cluster_id"], posting_id),
        )
        console.print(
            f"[bold]Same posting via {len(sibs)} other source(s):[/]"
            + (
                ""
                if r["is_cluster_canonical"]
                else "  [dim](this row is a sibling; the canonical row is marked ★)[/]"
            )
        )
        for sb in sibs:
            console.print(
                f"   {'★ ' if sb['is_cluster_canonical'] else '  '}#{sb['id']} {sb['source_provider']:<16} {sb['url_status']:<10} {sb['apply_url']}"
            )
    if r["repost_of_id"]:
        console.print(
            f"[yellow]Repost[/] of #{r['repost_of_id']} (same company/title/metro re-listed within 90 days)"
        )
    apps = db.all_rows(
        conn, "SELECT id, stage, applied_at FROM applications WHERE posting_id = ?", (posting_id,)
    )
    for a in apps:
        console.print(
            f"[bold green]Application #{a['id']}[/] stage={a['stage']} applied {a['applied_at']}"
        )
    events = db.all_rows(
        conn,
        "SELECT event_type, at, data_json FROM posting_events WHERE posting_id = ? ORDER BY at DESC LIMIT 12",
        (posting_id,),
    )
    if events:
        console.print(
            "[bold]Events:[/] "
            + " · ".join(f"{e['event_type']} {ago_human(e['at'])}" for e in events)
        )
    if r["description_md"]:
        console.rule("description")
        console.print(
            r["description_md"][:6000]
            + ("\n[dim]… (truncated)[/]" if len(r["description_md"]) > 6000 else "")
        )
    else:
        console.print(
            "[dim]No description stored (detail not fetched for this title — run `radar fetch --detail all_new` to fetch all).[/]"
        )


# ---------------------------------------------------------------------------------------------
# Workflow actions
# ---------------------------------------------------------------------------------------------


@app.command()
def applied(
    posting_id: int,
    referral: str | None = typer.Option(
        None, "--referral", help="Referral contact name (marks referral_used)"
    ),
    note: str | None = typer.Option(None, "--note"),
    applied_at: str | None = typer.Option(
        None, "--at", help="When you applied (default now), e.g. 2026-09-12"
    ),
    resume: str | None = typer.Option(None, "--resume", help="Resume version label"),
    force: bool = typer.Option(False, "--force", help="Create even if the duplicate guard fires"),
) -> None:
    """One tap: create the application record and remove the posting from the queue."""
    from radar.applications import DuplicateApplication, mark_applied

    conn = _conn()
    try:
        app_id = mark_applied(
            conn,
            posting_id,
            applied_at=applied_at,
            referral_used=bool(referral),
            referral_contact=referral,
            notes=note,
            resume_version=resume,
            force=force,
        )
    except DuplicateApplication as e:
        err.print(f"[yellow]duplicate guard:[/] {e.reason}")
        for a in e.existing:
            err.print(
                f"   application #{a['id']}: {a['company_name']} — {a['title']} ({a['stage']}, applied {a['applied_at'][:10]})"
            )
        err.print("   use --force to record it anyway")
        raise typer.Exit(3) from None
    except KeyError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    r = db.one(conn, "SELECT company_name, title FROM postings WHERE id = ?", (posting_id,))
    console.print(
        f"[green]✓ application #{app_id}[/] — {r['company_name']} — {r['title']}. Follow-up nudge in 10 business days."
    )


@app.command()
def dismiss(
    posting_id: int,
    reason: str = typer.Option(..., "--reason", "-r", help="Why (feeds calibration)"),
) -> None:
    """Dismiss a posting with a reason. It leaves the queue; the record stays."""
    conn = _conn()
    now = utcnow_iso()
    with db.transaction(conn):
        if not db.one(conn, "SELECT id FROM postings WHERE id = ?", (posting_id,)):
            err.print("[red]not found[/]")
            raise typer.Exit(1)
        db.update(
            conn,
            "postings",
            posting_id,
            {"status": "dismissed", "dismiss_reason": reason, "status_changed_at": now},
        )
        db.add_event(conn, posting_id, "status_changed", {"to": "dismissed", "reason": reason})
    from radar.score.views import stamp_view_flags

    stamp_view_flags(conn, [posting_id])
    console.print(f"[green]✓ dismissed #{posting_id}[/] ({reason})")


@app.command()
def shortlist(posting_id: int, undo: bool = typer.Option(False, "--undo")) -> None:
    """Shortlist (star) a posting."""
    conn = _conn()
    now = utcnow_iso()
    with db.transaction(conn):
        db.update(
            conn,
            "postings",
            posting_id,
            {
                "status": "new" if undo else "shortlisted",
                "starred": 0 if undo else 1,
                "status_changed_at": now,
            },
        )
        db.add_event(conn, posting_id, "status_changed", {"to": "new" if undo else "shortlisted"})
    from radar.score.views import stamp_view_flags

    stamp_view_flags(conn, [posting_id])
    console.print(f"[green]✓ {'un-' if undo else ''}shortlisted #{posting_id}[/]")


@app.command()
def snooze(posting_id: int, days: int = typer.Option(7, "--days", "-d")) -> None:
    """Hide a posting from the queue for N days."""
    from datetime import timedelta

    from radar.util import to_iso, utcnow

    conn = _conn()
    until = to_iso(utcnow() + timedelta(days=days))
    with db.transaction(conn):
        db.update(
            conn,
            "postings",
            posting_id,
            {"status": "snoozed", "snooze_until": until, "status_changed_at": utcnow_iso()},
        )
        db.add_event(conn, posting_id, "status_changed", {"to": "snoozed", "until": until})
    from radar.score.views import stamp_view_flags

    stamp_view_flags(conn, [posting_id])
    console.print(f"[green]✓ snoozed #{posting_id} until {until[:10]}[/]")


@app.command()
def referral(
    posting_id: int,
    contact: str = typer.Argument(..., help="Who's referring you"),
    undo: bool = typer.Option(False, "--undo"),
) -> None:
    """Log a secured referral for a posting (upgrades winnability/priority on rescore)."""
    conn = _conn()
    with db.transaction(conn):
        db.update(conn, "postings", posting_id, {"referral_secured": 0 if undo else 1})
        db.add_event(conn, posting_id, "referral_logged", {"contact": contact, "undo": undo})
    console.print(
        f"[green]✓ referral {'removed' if undo else 'logged'} for #{posting_id}[/] ({contact}). Run `radar rescore` to apply the ×1.05."
    )


@app.command()
def note(posting_id: int, text: str) -> None:
    """Append a note to a posting."""
    conn = _conn()
    r = db.one(conn, "SELECT notes_md FROM postings WHERE id = ?", (posting_id,))
    if not r:
        err.print("[red]not found[/]")
        raise typer.Exit(1)
    new = ((r["notes_md"] or "") + f"\n\n[{utcnow_iso()[:10]}] {text}").strip()
    with db.transaction(conn):
        db.update(conn, "postings", posting_id, {"notes_md": new})
        db.add_event(conn, posting_id, "note", {"text": text})
    console.print("[green]✓ note added[/]")


# ---------------------------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------------------------


@apps_app.command("list")
def apps_list(
    include_completed: bool = typer.Option(False, "--all", help="Include completed rows"),
) -> None:
    """Your applications, grouped by stage, with follow-ups due."""
    from radar.applications import funnel_stats, suggestions

    conn = _conn()
    rows = db.all_rows(
        conn,
        "SELECT * FROM applications "
        + ("" if include_completed else "WHERE completed = 0 ")
        + "ORDER BY stage, applied_at DESC",
    )
    st = funnel_stats(conn)
    console.print(
        f"[bold]{st['total']} applications[/] · {st['active']} active · {st['completed']} completed · response rate {('%.0f%%' % (100 * st['response_rate'])) if st['response_rate'] is not None else '—'} · median days to first response {f'{st["median_days_to_first_response"]:.0f}' if st['median_days_to_first_response'] else '—'}"
    )
    if st["by_stage"]:
        console.print("  by stage: " + " · ".join(f"{k} {v}" for k, v in st["by_stage"].items()))
    if st["by_source"]:
        console.print(
            "  by discovery source: " + " · ".join(f"{k} {v}" for k, v in st["by_source"].items())
        )
    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    for col in (
        "id",
        "stage",
        "company",
        "title",
        "location",
        "applied",
        "follow-up",
        "ref",
        "src",
        "link",
    ):
        t.add_column(col, overflow="fold")
    for a in rows:
        t.add_row(
            str(a["id"]),
            a["stage"],
            a["company_name"],
            a["title"],
            a["location"] or "",
            a["applied_at"][:10],
            (a["follow_up_due"] or "")[:10],
            "✓" if a["referral_used"] else "",
            (a["source_of_discovery"] or "")[:10],
            a["apply_url"] or "",
        )
    console.print(t)
    sug = suggestions(conn)
    if sug["follow_ups_due"]:
        console.print(
            "[yellow]Follow-ups due:[/] "
            + ", ".join(f"#{a['id']} {a['company_name']}" for a in sug["follow_ups_due"])
        )
    if sug["ghosted_candidates"]:
        console.print(
            "[dim]30+ days, no response — consider `radar apps stage <id> ghosted`:[/] "
            + ", ".join(f"#{a['id']} {a['company_name']}" for a in sug["ghosted_candidates"])
        )


@apps_app.command("add")
def apps_add(
    url: str | None = typer.Argument(
        None, help="Posting URL (autofills company/title/location when it can)"
    ),
    company: str | None = typer.Option(None, "--company", "-c"),
    title: str | None = typer.Option(None, "--title", "-t"),
    location: str | None = typer.Option(None, "--location", "-l"),
    applied_at: str | None = typer.Option(None, "--at", help="Default now"),
    stage: str = typer.Option("applied", "--stage"),
    note: str | None = typer.Option(None, "--note"),
    referral: str | None = typer.Option(None, "--referral"),
    source: str = typer.Option(
        "manual", "--source", help="Where you found it (e.g. simplify, linkedin, friend)"
    ),
    force: bool = typer.Option(False, "--force"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask to confirm autofilled fields"),
) -> None:
    """Record an application the system never saw. Give a URL and it fills in what it can."""
    from radar.applications import DuplicateApplication, add_manual, autofill_from_url

    cfg = get_config()
    conn = _conn(cfg)
    posting_id = None
    if url and not (company and title and location):
        console.print("[dim]looking up URL…[/]")
        af = asyncio.run(autofill_from_url(conn, url, cfg.fetch.user_agent))
        company = company or af.company_name
        title = title or af.title
        location = location or af.location
        posting_id = af.posting_id
        if af.note:
            console.print(f"[yellow]{af.note}[/]")
        else:
            console.print(
                f"[dim]autofill via {af.source} (confidence {af.confidence:.0%}):[/] {company} — {title} — {location or '?'}"
                + (f"  [dim](matches posting #{posting_id})[/]" if posting_id else "")
            )
    if not company:
        company = typer.prompt("Company")
    if not title:
        title = typer.prompt("Title")
    if location is None and not yes:
        location = typer.prompt("Location", default="", show_default=False) or None
    if (
        not yes
        and url
        and not typer.confirm(
            f"Record: {company} — {title} ({location or 'location ?'}) stage={stage}?", default=True
        )
    ):
        raise typer.Exit(0)
    try:
        app_id = add_manual(
            conn,
            url=url,
            company_name=company,
            title=title,
            location=location,
            applied_at=applied_at,
            stage=stage,
            notes=note,
            referral_used=bool(referral),
            referral_contact=referral,
            source_of_discovery=source,
            posting_id=posting_id,
            force=force,
        )
    except DuplicateApplication as e:
        err.print(f"[yellow]duplicate guard:[/] {e.reason} (use --force to record anyway)")
        raise typer.Exit(3) from None
    except ValueError as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(2) from None
    console.print(f"[green]✓ application #{app_id}[/] — {company} — {title}")


@apps_app.command("stage")
def apps_stage(
    app_id: int,
    stage: str,
    note: str | None = typer.Option(None, "--note"),
    base: float | None = typer.Option(None, "--base", help="Base offered (for offer stage)"),
) -> None:
    """Move an application to a new stage: applied | oa_pending | oa_done | screen | onsite | offer | rejected | ghosted | withdrawn."""
    from radar.applications import set_stage

    conn = _conn()
    try:
        set_stage(conn, app_id, stage, note=note, base_offered=base)
    except (ValueError, KeyError) as e:
        err.print(f"[red]{e}[/]")
        raise typer.Exit(2) from None
    console.print(f"[green]✓ #{app_id} → {stage}[/]")


@apps_app.command("done")
def apps_done(
    app_id: int,
    outcome: str | None = typer.Option(None, "--outcome"),
    reopen: bool = typer.Option(False, "--reopen"),
) -> None:
    """Mark 'I'm done acting on this' (leaves active views, stays in the tracker forever)."""
    from radar.applications import set_completed

    conn = _conn()
    set_completed(conn, app_id, completed=not reopen, outcome=outcome)
    console.print(f"[green]✓ #{app_id} {'reopened' if reopen else 'completed'}[/]")


@apps_app.command("export")
def apps_export(out: Path = typer.Option(Path("data/applications.csv"), "--out", "-o")) -> None:
    """Export every application to CSV (your record survives this project)."""
    conn = _conn()
    rows = db.all_rows(conn, "SELECT * FROM applications ORDER BY applied_at")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))
    console.print(f"wrote {len(rows)} applications → {out}")


# ---------------------------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------------------------


@app.command()
def rescore(
    company: str | None = typer.Option(None, "--company", "-c"),
    provider: list[str] = typer.Option(
        None, "--provider", "-p", help="Replay only these providers"
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Score every row, not just queue-eligible ones (after scope-rule edits)",
    ),
    replay: bool = typer.Option(
        False,
        "--replay",
        help="Also re-parse stored raw payloads through the current adapters/normalization rules (needed after editing title_rules.yaml or metros.yaml; ~90s for 30k postings). Weight/config changes don't need it.",
    ),
) -> None:
    """Recompute scores from stored data and current config — zero re-fetching."""
    from radar.rescore import rescore as _rescore

    cfg = get_config()
    conn = _conn(cfg)
    out = _rescore(
        conn,
        cfg,
        replay=replay,
        company=company,
        providers=set(provider) if provider else None,
        full=full,
    )
    rp = out.get("replay")
    if rp:
        console.print(
            f"[green]replay[/] {rp['sources']} sources · {rp['replayed']} postings re-derived · {rp['elapsed_s']}s"
            + (f"\n  skipped: {rp['skipped']}" if rp["skipped"] else "")
        )
    sc = out.get("score") or {}
    if isinstance(sc, dict) and sc.get("scored") is not None:
        console.print(
            f"[green]score[/] {sc['scored']} postings · {sc['in_scope']} in scope · {sc['queue']} in queue · verdicts {sc.get('verdicts')} · {sc['elapsed_s']}s"
        )
    else:
        console.print(f"[green]score[/] {sc}")
    cl = out.get("clustering") or {}
    if cl:
        console.print(
            f"[green]clusters[/] {cl.get('multi_clusters')} multi-source · {cl.get('siblings_hidden')} duplicates folded · {cl.get('reposts_linked')} reposts"
        )


def _run_verify(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    limit: int = 500,
    all_rows: bool = False,
    posting_ids: list[int] | None = None,
) -> None:
    from radar.links import sweep

    changes: list[str] = []

    def progress(row: sqlite3.Row, v: Any, changed: bool) -> None:
        if changed:
            changes.append(
                f"#{row['id']} {row['company_name']} — {row['title'][:40]}: {row['url_status']} → {v.status} ({v.reason})"
            )

    stats = asyncio.run(
        sweep(conn, cfg, limit=limit, all_rows=all_rows, posting_ids=posting_ids, progress=progress)
    )
    console.print(
        f"[bold]verified {stats.checked}[/]: [green]{stats.live} live[/] · {stats.redirected} redirected · [red]{stats.dead} dead[/] · {stats.unverified} unverified · {stats.changed} status changes"
    )
    for c in changes[:40]:
        console.print("  " + c)


@app.command("verify-links")
def verify_links(
    limit: int = typer.Option(500, "--limit", "-n"),
    everything: bool = typer.Option(False, "--all", help="Re-verify everything, oldest first"),
    ids: list[int] = typer.Option(None, "--id", help="Specific posting ids (repeatable)"),
) -> None:
    """Sweep apply links: API check where the ATS supports it, else HTTP with redirect-to-generic detection."""
    cfg = get_config()
    conn = _conn(cfg)
    _run_verify(conn, cfg, limit=limit, all_rows=everything, posting_ids=ids or None)


@app.command()
def cycle(
    provider: list[str] = typer.Option(None, "--provider", "-p"),
    tier1_only: bool = typer.Option(
        False, "--tier1-only", help="Only 15-minute sources (dream list, Tier-1, aggregators)"
    ),
    skip_verify: bool = typer.Option(False, "--skip-verify"),
    skip_discover: bool = typer.Option(False, "--skip-discover"),
    skip_enrich: bool = typer.Option(False, "--skip-enrich", help="Skip the per-cycle LLM budget"),
    host: str = typer.Option("laptop", "--host", help="laptop | actions (recorded on runs)"),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="One summary line only (for schedulers)"
    ),
) -> None:
    """One scheduled cycle: fetch what's due → link sweep (if due) → discovery (if due) → score. Safe to run any time."""
    from radar.scheduler import run_cycle_sync

    cfg = get_config()
    conn = _conn(cfg)
    progress = (
        None
        if quiet
        else (
            lambda o: console.print(
                f"  {'✓' if o.ok else '✗'} {o.spec.company_name[:24]:<24} {o.spec.provider:<14} {o.mode:<11} rows={o.rows:<5} new={o.new:<4} delist={o.delisted:<3}"
                + (f" [red]{o.error}[/]" if o.error else "")
            )
        )
    )
    rep = run_cycle_sync(
        conn,
        cfg,
        host=host,
        providers=set(provider) if provider else None,
        tier1_only=tier1_only,
        skip_verify=skip_verify,
        skip_discover=skip_discover,
        skip_enrich=skip_enrich,
        progress=progress,
    )
    console.print(
        f"[bold]cycle[/] {rep.started_at}: {rep.fetched_sources} sources fetched · [green]{rep.new} new[/] · {rep.changed} changed · {rep.delisted} delisted"
        + (f" · [red]{rep.failed_sources} failed[/]" if rep.failed_sources else "")
        + (f" · [red]{rep.drift_alarms} drift[/]" if rep.drift_alarms else "")
        + (f" · {rep.verified} links verified ({rep.link_changes} changed)" if rep.verified else "")
        + (f" · {rep.discovered} employers discovered" if rep.discovered else "")
        + (f" · scored {rep.scored}" if rep.scored else "")
        + (f" · [red]alarms: {'; '.join(rep.alarms)}[/]" if rep.alarms else "")
        + (f" · LLM {rep.llm['calls']} calls ({rep.llm['cache_hits']} cached)" if rep.llm else "")
        + (f" · alerts {rep.notified['sent']} {rep.notified['by_tier']}" if rep.notified else "")
        + f" · {rep.elapsed_s}s"
        + (f"\n  [yellow]{'; '.join(rep.notes)}[/]" if rep.notes else "")
    )
    for name, error in rep.failures:
        err.print(f"  [red]✗ {name}:[/] {error}")
    if rep.status == "failed":
        # a critical subsystem (fetch / score / notify) did not run: make launchd and humans see it
        raise typer.Exit(1)


# ---------------------------------------------------------------------------------------------
# Scoring, queue, calendar, enrichment
# ---------------------------------------------------------------------------------------------

ACTION_LABEL = {
    "apply_today": "[bold green]Apply today[/]",
    "apply_this_week": "[green]Apply this week[/]",
    "watch": "[dim]Watch[/]",
    "get_referral_first": "[cyan]Get a referral first[/]",
    "blocked_needs_prep": "[yellow]Blocked, needs prep[/]",
    "verify_link": "[red]Link dead — verify[/]",
    "needs_review": "[dim]Needs review (unenriched)[/]",
}
VERDICT_LABEL = {
    "clearly_better": "[bold green]clearly better[/]",
    "arguably_better": "[yellow]arguably better[/]",
    "worse": "[red]worse[/]",
}


def _fmt_base_est(r: sqlite3.Row) -> str:
    if r["base_posted_min"] or r["base_posted_max"]:
        lo, hi = r["base_posted_min"], r["base_posted_max"]
        return (
            f"${lo / 1000:.0f}–{hi / 1000:.0f}k posted"
            if lo and hi and lo != hi
            else f"${(hi or lo) / 1000:.0f}k posted"
        )
    if r["base_est"]:
        return f"[dim]~${r['base_est'] / 1000:.0f}k est ({r['comp_confidence']:.0%})[/]"
    return "[dim]no comp signal[/]"


@app.command()
def queue(
    limit: int = typer.Option(25, "--limit", "-n"),
    action: str | None = typer.Option(
        None,
        "--action",
        help="apply_today | apply_this_week | watch | get_referral_first | blocked_needs_prep",
    ),
) -> None:
    """The Apply-First Queue (default view): what's left to do, best first. Applied/dismissed/snoozed rows are gone."""
    conn = _conn()
    where = "p.apply_priority_rank IS NOT NULL"
    params: list[Any] = []
    if action:
        where += " AND p.queue_action = ?"
        params.append(action)
    if not action:
        where += " AND p.queue_action != 'verify_link'"  # dead links get their own group below
    rows = db.all_rows(
        conn,
        f"SELECT p.* FROM postings p WHERE {where} ORDER BY p.apply_priority_rank LIMIT ?",
        [*params, limit],
    )
    dead = (
        db.all_rows(
            conn,
            "SELECT p.* FROM postings p WHERE p.apply_priority_rank IS NOT NULL AND p.queue_action = 'verify_link' ORDER BY p.apply_priority_rank LIMIT 10",
        )
        if not action
        else []
    )
    total = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE apply_priority_rank IS NOT NULL")
    today = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE queue_action = 'apply_today'")
    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    for col, j in (
        ("#", "right"),
        ("action", "left"),
        ("company", "left"),
        ("title", "left"),
        ("where", "left"),
        ("base", "right"),
        ("effective", "right"),
        ("verdict", "left"),
        ("score", "right"),
        ("urg", "right"),
        ("link", "left"),
    ):
        t.add_column(col, justify=j, overflow="fold", max_width=44 if col == "title" else None)
    for r in rows:
        t.add_row(
            str(r["apply_priority_rank"]),
            ACTION_LABEL.get(r["queue_action"] or "", r["queue_action"] or ""),
            ("★ " if r["is_dream_list"] else "") + r["company_name"],
            r["title"],
            _fmt_loc(r),
            _fmt_base_est(r),
            f"${r['effective_value'] / 1000:.0f}k" if r["effective_value"] else "—",
            VERDICT_LABEL.get(r["beats_baseline"] or "", r["beats_baseline"] or "—"),
            f"{r['composite_score']:.2f}",
            f"{r['urgency_score']:.2f}",
            _fmt_link(r),
        )
    console.print(t)
    if dead:
        console.print(
            f"[red bold]Link dead — verify before dismissing ({len(dead)})[/]  [dim]these would be in the queue; the employer's page stopped serving the req. `radar restore-link <id>` if it is open after all, `radar dismiss <id> --reason closed` if not.[/]"
        )
        for r in dead:
            console.print(
                f"  #{r['apply_priority_rank']:<5} {('★ ' if r['is_dream_list'] else '') + r['company_name']:<24} {r['title'][:48]:<48} {_fmt_link(r)}  id {r['id']}"
            )
    console.print(
        f"[bold]{len(rows)} of {total}[/] in the queue · [green]{today} in today's bucket[/] (cap {get_config().throughput.today_bucket_max}) · `radar show <id>` for the full decomposition · `radar applied <id>` when done"
    )


@app.command("restore-link")
def restore_link(posting_id: int = typer.Argument(...)) -> None:
    """Dead-link group → "still live, restore": re-verify; if the verifier still says dead, your word wins (recorded as a manual verdict)."""
    import asyncio

    from radar.links import sweep
    from radar.score.engine import score_all
    from radar.workflow import apply_action

    cfg = get_config()
    conn = _conn(cfg)
    asyncio.run(sweep(conn, cfg, posting_ids=[posting_id]))
    status = db.scalar(conn, "SELECT url_status FROM postings WHERE id = ?", (posting_id,))
    if status in ("live", "redirected"):
        conn.execute("UPDATE postings SET needs_rescore = 1 WHERE id = ?", (posting_id,))
        console.print(f"[green]verified {status}[/] — back in the queue")
    else:
        r = apply_action(conn, posting_id, "restore_link", via="cli")
        if not r.ok:
            err.print(f"[red]{r.error}[/]")
            raise typer.Exit(1)
        console.print(
            "verifier still says dead; recorded your manual verdict (live) — back in the queue until the next sweep finds evidence otherwise"
        )
    score_all(conn, cfg, ids=[posting_id])


@app.command()
def calendar() -> None:
    """The decision calendar: the baseline decision deadline (honestly), the switching-cost curve, and the season tracker."""
    from radar.score.engine import decision_calendar

    cfg = get_config()
    cal = decision_calendar(cfg)
    console.rule("[bold]Decision calendar")
    console.print(
        f"[bold]Baseline decision deadline:[/] {cal['baseline_decision_deadline']}  ([red]{cal['days_to_deadline']} days[/])"
    )
    console.print(f"  {cal['deadline_note']}")
    console.print(
        f"[bold]Baseline start:[/] {cal['baseline_start']}   switching window {cal['switching_window']['from']} → {cal['switching_window']['to']}; cheap zone ends {cal['switching_window']['cheap_zone_ends']}"
    )
    t = Table(
        box=box.SIMPLE_HEAD,
        header_style="bold",
        title="Cost of walking away from the baseline, by date (itemized in config.switching_friction — zero any term you disagree with)",
    )
    for col in ("date", "clawback", "goodwill", "university", "total", "zone"):
        t.add_column(col, justify="right" if col != "zone" else "left")
    for pt in cal["switching_window"]["curve"]:
        it = pt["items"]
        t.add_row(
            pt["date"],
            f"${it['signing_bonus_clawback']:,.0f}",
            f"${it['goodwill']:,.0f}",
            f"${it['university_channel']:,.0f}",
            f"[bold]${pt['total']:,.0f}[/]",
            "[green]cheap[/]" if pt["cheap_zone"] else "[yellow]rising[/]",
        )
    console.print(t)
    s = cal["season"]
    console.print(
        f"[bold]Season:[/] {s['start']} → {s['end']}, {s['elapsed_fraction']:.0%} elapsed.  {s['note']}"
    )
    console.print(
        f"[dim]Today's switching friction: ${cal['switching_window']['today']['total']:,.0f}.[/]"
    )


@app.command()
def enrich(
    calls: int = typer.Option(
        None, "--calls", help="Max LLM calls this run (default llm.max_calls_per_run)"
    ),
) -> None:
    """LLM enrichment via the headless CLI (classifier model: requirements; enrichment model: resume gaps). Cached, budgeted, disableable."""
    from radar.enrich.pipeline import enrich as _enrich

    cfg = get_config()
    conn = _conn(cfg)
    run_id = db.start_run(conn, "enrich")
    try:
        res = _enrich(conn, cfg, max_calls=calls, run_id=run_id)
    except Exception as e:
        db.finish_run(conn, run_id, status="failed", error=str(e))
        raise
    db.finish_run(conn, run_id, stats=res)
    console.print(json.dumps(res, indent=2))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8787, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev)"),
) -> None:
    """Start the local dashboard + API (http://127.0.0.1:8787). Build the UI once with `cd web && npm run build`."""
    from radar.serve.api import serve as _serve

    console.print(f"Job Radar → http://{host}:{port}   (API docs at /api/docs; Ctrl-C to stop)")
    _serve(host=host, port=port, reload=reload)


@app.command("fetch-descriptions")
def fetch_descriptions(limit: int = typer.Option(40, "--limit", "-n")) -> None:
    """Read employer pages for aggregator-only queue rows that lack a description (robots.txt-respecting)."""
    from radar.fetch.html_detail import fetch_missing

    cfg = get_config()
    conn = _conn(cfg)
    run_id = db.start_run(conn, "html_detail")
    st = asyncio.run(fetch_missing(conn, cfg, limit=limit, run_id=run_id))
    db.finish_run(conn, run_id, stats=st)
    console.print(st)
    if st.get("fetched"):
        from radar.score.engine import score_all

        console.print(score_all(conn, cfg, only_unscored=True))


@app.command("lca-refresh")
def lca_refresh(
    fiscal: str | None = typer.Option(
        None, "--fiscal", help="e.g. FY2026_Q3 (default: newest on the DOL page)"
    ),
) -> None:
    """Download + ingest the DOL LCA disclosure file (~250 MB; a few minutes). Quarterly."""
    from radar.enrich.lca import refresh

    cfg = get_config()
    conn = _conn(cfg)
    res = refresh(
        conn,
        cfg,
        fiscal=fiscal,
        progress=lambda seen, kept: console.print(f"  … {seen:,} rows scanned, {kept:,} kept"),
    )
    console.print(res)


# ---------------------------------------------------------------------------------------------
# Notifications (§20)
# ---------------------------------------------------------------------------------------------


@app.command()
def notify(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Evaluate triggers and anti-noise, send nothing"
    ),
    since: str | None = typer.Option(None, "--since", help="ISO time; default = last notify run"),
    poll: bool = typer.Option(True, "--poll/--no-poll", help="Drain Telegram button taps first"),
    test: int | None = typer.Option(
        None,
        "--test",
        help="Send one P0-shaped alert for this posting id (recorded as tier 'test'; caps/dedupe untouched)",
    ),
) -> None:
    """Send P0/P1 alerts for newly seen postings (Telegram first), after applying Telegram button taps."""
    from radar.notify.engine import send_alerts
    from radar.notify.telegram_actions import drain_and_apply

    cfg = get_config()
    conn = _conn(cfg)
    if test is not None:
        import json as _json

        from radar.notify.channels import all_channels
        from radar.notify.engine import payload_for

        row = db.one(conn, "SELECT * FROM postings WHERE id = ?", (test,))
        if not row:
            err.print("[red]no such posting[/]")
            raise typer.Exit(1)
        p = dict(row)
        payload = payload_for(
            p, "p0", "TEST — " + (p.get("beats_baseline_reason") or "manual test send"), cfg
        )
        sent = []
        for ch in all_channels(str(cfg.data_dir / "notifications.log")):
            if ch.available() and ch.name in ("telegram", "file"):
                ext = ch.send(payload)
                if ext:
                    sent.append(ch.name)
        db.insert(
            conn,
            "notifications",
            {
                "posting_id": test,
                "tier": "test",
                "trigger": "manual_test",
                "channel": ",".join(sent),
                "sent_at": utcnow_iso(),
                "payload_json": _json.dumps(
                    {"title": payload.title, "lines": payload.body_lines, "url": payload.url}
                ),
                "reason": "manual test",
            },
        )
        console.print(
            f"test alert sent via {sent or 'nothing (no channel)'}:\n  {payload.title}\n  "
            + "\n  ".join(payload.body_lines)
            + f"\n  {payload.url}\n  buttons: {[b[0] for b in payload.buttons]}"
        )
        return
    run_id = db.start_run(conn, "notify")
    if poll:
        from radar.notify.telegram_actions import listener_owns_queue

        if listener_owns_queue(cfg):
            console.print(
                "telegram: the listener agent owns the update queue (taps are applied live); not draining here"
            )
        else:
            console.print(f"telegram: {drain_and_apply(conn)}")
    st = send_alerts(conn, cfg, since=since, dry_run=dry_run, run_id=run_id)
    db.finish_run(conn, run_id, stats=st.__dict__)
    if st.channels == ["seeded"]:
        console.print(
            "First notify run: watermark set, nothing sent — the existing backlog lives in the dashboard/queue. "
            "Postings first seen from now on are eligible for alerts (use --since to force a window)."
        )
        return
    console.print(
        f"[bold]{'DRY RUN — ' if dry_run else ''}{st.sent} sent[/] of {st.candidates} candidates via {st.channels or 'no channel configured (file log only)'} · by tier {st.by_tier}"
    )
    if st.suppressed:
        console.print("  suppressed: " + " · ".join(f"{k}: {v}" for k, v in st.suppressed.items()))
    for line in st.would_send[:20]:
        console.print(f"  would send: {line}")


@app.command("telegram-setup")
def telegram_setup(
    rediscover: bool = typer.Option(
        False, "--rediscover", help="Look the chat id up again even if one is stored"
    ),
) -> None:
    """Idempotent. Stores TELEGRAM_CHAT_ID the instant it is read from the bot's update queue (before
    anything else can fail), never calls getUpdates once a chat id is stored (the queue is destructive
    on read), and sends a confirmation. Needs TELEGRAM_BOT_TOKEN (`radar secret set`)."""
    import os

    from radar.notify.channels import Payload, Telegram
    from radar.notify.telegram_actions import listener_owns_queue
    from radar.secrets import set_secret

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        err.print("[red]set the token first: radar secret set TELEGRAM_BOT_TOKEN[/]")
        raise typer.Exit(1)
    cfg = get_config()
    tg = Telegram()
    me = tg.get_me()
    console.print(f"bot: @{me.get('username')} (id {me.get('id')})")
    if os.environ.get("TELEGRAM_CHAT_ID") and not rediscover:
        console.print(
            "chat id already stored — not touching the update queue (use --rediscover to look again)"
        )
    else:
        if listener_owns_queue(cfg):
            # the listener is draining the queue and will capture the chat id itself (_remember_chat)
            err.print(
                "[yellow]the telegram-listen agent is running and owns the queue; it stores the chat id from your next message automatically. Stop it first if you want setup to read the queue itself.[/]"
            )
            raise typer.Exit(1)
        found = tg.discover_chat_id()  # no offset → reads without confirming anything
        if not found:
            err.print(
                "[red]no private chat found in the bot's queue — send the bot any message, then rerun[/]"
            )
            raise typer.Exit(1)
        chat_id, who = found
        set_secret(
            "TELEGRAM_CHAT_ID", chat_id
        )  # FIRST thing after reading it; nothing below can lose it
        console.print(
            f"chat id stored for @{who} (value not shown; `radar secret list` shows keys only)"
        )
    tg2 = Telegram()
    ok = tg2.send(
        Payload(
            tier="system",
            title="Job Radar connected",
            body_lines=[
                "This chat will receive P0/P1 alerts with inline buttons, the 08:00 digest, and the Sunday weekly."
            ],
            html=False,
        )
    )
    console.print("confirmation message sent" if ok else "[red]confirmation send failed[/]")


@app.command("telegram-webhook")
def telegram_webhook(
    what: str = typer.Argument("status", help="status | reconcile | drain | off"),
) -> None:
    """Webhook mode via the Cloudflare Worker (deploy/cloudflare). `reconcile` picks the single
    consumer (webhook if the Worker is healthy, else polling) and makes Telegram agree; `drain`
    applies queued taps now; `off` deletes the webhook and returns to polling."""
    from radar.notify.channels import Telegram
    from radar.notify.telegram_webhook import (
        configured,
        current_mode,
        delete_webhook,
        drain_webhook,
        reconcile,
        webhook_info,
        worker_healthy,
    )

    cfg = get_config()
    conn = _conn(cfg)
    tg = Telegram()
    if not tg.token:
        err.print("[red]TELEGRAM_BOT_TOKEN not set[/]")
        raise typer.Exit(1)
    if what == "status":
        info = webhook_info(tg)
        console.print(
            f"mode: {current_mode(conn)} · configured: {configured()} · worker healthy: {worker_healthy() if configured() else 'n/a'}\n"
            f"telegram webhook url: {info.get('url') or '(none — polling)'} · pending at telegram: {info.get('pending_update_count', 0)}"
            + (
                f" · last error: {info.get('last_error_message')}"
                if info.get("last_error_message")
                else ""
            )
        )
    elif what == "reconcile":
        console.print(reconcile(conn, tg))
    elif what == "drain":
        console.print(drain_webhook(conn, tg))
    elif what == "off":
        delete_webhook(tg)
        db.kv_set(conn, "telegram_mode", "polling")
        console.print("webhook deleted; polling mode")
    else:
        raise typer.Exit(2)


@app.command("telegram-listen")
def telegram_listen(
    once: bool = typer.Option(False, "--once", help="One long-poll round, then exit (for tests)"),
) -> None:
    """Long-poll Telegram for button taps and apply them immediately (run by launchd, KeepAlive)."""
    from radar.notify.telegram_actions import listen

    cfg = get_config()
    conn = _conn(cfg)
    listen(conn, once=once)


@app.command()
def digest(
    kind: str = typer.Argument("daily", help="daily | weekly | wake"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print instead of sending"),
) -> None:
    """Build and send the daily (P2) or weekly (P3) digest; `wake` prints the catch-up summary."""
    from radar.notify.digest import send_digest, wake_summary

    cfg = get_config()
    conn = _conn(cfg)
    if kind == "wake":
        since = db.kv_get(conn, "last_cycle_at") or utcnow_iso()
        p = wake_summary(conn, cfg, since=since)
        console.print(p.text() if p else "nothing new since the last cycle")
        return
    res = send_digest(conn, cfg, kind, dry_run=dry_run)
    console.print(
        res["text"] if dry_run else f"{kind} digest → {res['channels']} ({res['lines']} lines)"
    )


@app.command()
def ics(out: Path = typer.Option(Path("data/radar.ics"), "--out", "-o")) -> None:
    """Write the deadline/follow-up/baseline-dates calendar feed (also served at /api/calendar.ics)."""
    from radar.notify.digest import ics_feed

    cfg = get_config()
    conn = _conn(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ics_feed(conn, cfg))
    console.print(
        f"wrote {out} — subscribe to http://127.0.0.1:8787/api/calendar.ics while `radar serve` runs"
    )


# ---------------------------------------------------------------------------------------------
# GitHub Actions side (no database)
# ---------------------------------------------------------------------------------------------


@app.command("actions-cycle")
def actions_cycle(
    actions_dir: Path = typer.Option(
        Path("actions"), "--dir", help="Where state/deltas live (committed)"
    ),
    detail: str = typer.Option(
        "none", "--detail", help="Detail-fetch policy for new rows: none | title_prefilter"
    ),
) -> None:
    """Run one cloud fetch cycle: Tier-1 boards + aggregator repos → deltas + pre-score. No DB, no LLM."""
    from radar.actions_runner import run_actions_cycle

    cfg = get_config()
    st = asyncio.run(run_actions_cycle(cfg, actions_dir=actions_dir, detail_policy=detail))
    console.print(
        f"[bold]actions cycle[/] {st.sources} sources · {st.ok} ok ({st.not_modified} unchanged) · {st.failed} failed · "
        f"[green]{st.new} new[/] · P0 {st.p0} · P1 {st.p1} · {st.requests} requests · {st.elapsed_s}s"
    )
    if st.failed:
        # any failed source is a failed job: a half-fetched cycle must not look green in the Actions tab
        err.print(
            f"[red]{st.failed} source(s) failed — see above; exiting non-zero so the run is visible[/]"
        )
        raise typer.Exit(1)


@app.command("actions-notify")
def actions_notify(actions_dir: Path = typer.Option(Path("actions"), "--dir")) -> None:
    """Cloud side: send P0/P1 for fresh (non-seed) delta rows via Telegram, drain button taps to actions/actions.jsonl."""
    from radar.actions_runner import notify_from_deltas

    cfg = get_config()
    st = notify_from_deltas(cfg, actions_dir=actions_dir)
    console.print(f"actions notify: {st}")
    if st.get("channel") == "none":
        console.print(
            "no notification channel configured (TELEGRAM_BOT_TOKEN/CHAT_ID unset) — explicit no-op, exit 0"
        )
        return
    if st.get("failed"):
        err.print(f"[red]{st['failed']} alert(s) failed to deliver — exiting non-zero[/]")
        raise typer.Exit(1)


@app.command("actions-usage")
def actions_usage(
    actions_dir: Path = typer.Option(Path("actions"), "--dir"),
    billing: bool = typer.Option(
        False,
        "--billing",
        help="Also query GitHub's billing API (GITHUB_BILLING_TOKEN + actions.github_user)",
    ),
) -> None:
    """Raw count of Actions invocations this month (a floor on billed minutes — GitHub rounds every
    job up to a minute). With --billing, the real number from GitHub. No estimates (D52)."""
    import os

    from radar.actions_runner import billing_from_github, invocations_this_month

    cfg = get_config()
    n = invocations_this_month(actions_dir)
    target = cfg.actions.invocation_target_per_month
    console.print(
        f"Actions invocations this month: {n} (target < {target}; each bills ≥ 1 minute of the 2,000 free)"
    )
    if n >= target:
        err.print(
            f"[red]over target: {n} ≥ {target} — thin the schedule in .github/workflows/fetch.yml[/]"
        )
    if billing:
        token = os.environ.get("GITHUB_BILLING_TOKEN")
        if not token or not cfg.actions.github_user:
            err.print(
                "[red]--billing needs GITHUB_BILLING_TOKEN (PAT with read:user) and actions.github_user[/]"
            )
            raise typer.Exit(2)
        b = billing_from_github(token, cfg.actions.github_user)
        console.print(
            f"GitHub says: {b['total_minutes_used']} minutes used of {b['included_minutes']} included "
            f"({b['total_paid_minutes_used']} paid) · breakdown {b['minutes_used_breakdown']}"
        )
    if n >= target:
        raise typer.Exit(1)


@app.command("ingest-deltas")
def ingest_deltas(
    actions_dir: Path = typer.Option(Path("actions"), "--dir"),
    days: int = typer.Option(14, "--days", help="Look back this many delta files"),
) -> None:
    """Laptop side: fold Actions deltas into the DB (backdate first_seen_at; insert rows not fetched yet)."""
    from radar.actions_ingest import ingest

    cfg = get_config()
    conn = _conn(cfg)
    res = ingest(conn, cfg, actions_dir=actions_dir, days=days)
    console.print(
        f"deltas: {res['files']} files · {res['lines']} lines · {res['backdated']} first_seen backdated · {res['inserted']} provisional rows inserted · {res['already']} already known"
    )


@app.command()
def discover(
    limit: int = typer.Option(300, "--limit", "-n", help="Max queued employers to process"),
    min_seen: int = typer.Option(
        1, "--min-seen", help="Only employers seen at least this many times in aggregators"
    ),
) -> None:
    """Turn employers seen in aggregator feeds into registry sources (detect ATS → probe → add)."""
    from radar.discover import run_discovery
    from radar.fetch.registry import load_registry, sync_registry

    cfg = get_config()
    conn = _conn(cfg)
    pending = db.scalar(conn, "SELECT COUNT(*) FROM discovery_queue WHERE status = 'pending'")
    console.print(f"{pending} employers pending discovery; processing up to {limit}…")

    def progress(row: sqlite3.Row, outcome: str, detail: Any) -> None:
        colour = {
            "added": "green",
            "linked": "cyan",
            "unresolvable": "dim",
            "probe_failed": "yellow",
        }[outcome]
        console.print(f"  [{colour}]{outcome:<12}[/] {row['company_name'][:34]:<34} {detail or ''}")

    stats = asyncio.run(run_discovery(conn, cfg, limit=limit, min_seen=min_seen, progress=progress))
    console.print(
        f"\n[bold]processed {stats.processed}[/]: [green]{stats.resolved} added[/] · {stats.linked_existing} linked to existing · "
        f"{stats.unresolvable} unresolvable (no supported ATS) · {stats.failed_probe} probe failures"
    )
    if stats.resolved:
        load_registry.cache_clear()
        res = sync_registry(conn, cfg)
        console.print(
            f"registry now {res['companies']} companies / {res['sources']} sources (config/companies.discovered.yaml updated)"
        )


@app.command()
def health() -> None:
    """Exit non-zero on stale sources, failing sources, link-verification backlog, or model-pinning violations."""
    cfg = get_config()
    conn = _conn(cfg)
    problems: list[str] = []
    warnings: list[str] = []
    # model pinning (§18) — config validation already asserts; repeat explicitly
    from radar.config import assert_model_allowed_for_scheduled_work

    for task, model in cfg.llm.models().items():
        try:
            assert_model_allowed_for_scheduled_work(model)
        except Exception as e:
            problems.append(f"model pinning: {task} → {e}")
    stale = db.all_rows(
        conn,
        "SELECT c.name, cs.provider, cs.cadence, cs.last_success_at, cs.consecutive_failures, cs.last_error FROM company_sources cs JOIN companies c ON c.id = cs.company_id "
        "WHERE cs.enabled = 1 AND ((cs.last_success_at IS NULL AND julianday('now') - julianday(cs.detected_at) > 1) OR julianday('now') - julianday(cs.last_success_at) > "
        "CASE cs.cadence WHEN '15min' THEN 2/24.0 WHEN 'hourly' THEN 6/24.0 ELSE 1.5 END OR cs.consecutive_failures >= 3)",
    )
    if len(stale) > 8:
        last_cycle = db.kv_get(conn, "last_cycle_at")
        failing = [s for s in stale if s["consecutive_failures"] >= 3]
        problems.append(
            f"{len(stale)} sources stale ({len(failing)} actually failing) — last cycle {ago_human(last_cycle) if last_cycle else 'never'}; "
            "if nothing is scheduled run `radar install-launchd`"
        )
        stale = failing[:8]
    for s in stale:
        problems.append(
            f"source stale/failing: {s['name']} ({s['provider']}, {s['cadence']}) last ok {ago_human(s['last_success_at'])} failures={s['consecutive_failures']} {s['last_error'] or ''}"
        )
    backlog = db.scalar(
        conn,
        "SELECT COUNT(*) FROM postings p WHERE p.delisted_at IS NULL "
        "AND (EXISTS (SELECT 1 FROM applications a WHERE a.posting_id = p.id) OR p.queue_action = 'apply_today' OR p.status = 'shortlisted' OR p.apply_priority_rank <= 500) "
        "AND (p.url_last_verified_at IS NULL OR julianday('now') - julianday(p.url_last_verified_at) > 3)",
    )
    if backlog and backlog > 250:
        problems.append(
            f"link verification backlog: {backlog} act-on postings (applications/Today/shortlist/top-500) without a fresh check — the sweep is not keeping up where it matters"
        )
    from radar.ops import alarms as _alarms

    for a in _alarms.evaluate(conn, cfg):
        (problems if a.severity == "error" else warnings).append(f"{a.key}: {a.title} — {a.detail}")
    try:
        qc = conn.execute("PRAGMA quick_check").fetchone()[0]
        if qc != "ok":
            problems.append(f"sqlite quick_check: {qc}")
    except sqlite3.Error as e:
        problems.append(f"sqlite quick_check failed: {e}")
    last_run = db.one(
        conn,
        "SELECT kind, started_at, status, stats_json, llm_calls, llm_models_json FROM runs ORDER BY id DESC LIMIT 1",
    )
    llm_total = db.scalar(conn, "SELECT COALESCE(SUM(llm_calls),0) FROM runs") or 0
    llm_models = db.all_rows(conn, "SELECT llm_models_json FROM runs WHERE llm_calls > 0")
    per_model: dict[str, int] = {}
    for r in llm_models:
        for k, v in json.loads(r["llm_models_json"] or "{}").items():
            per_model[k] = per_model.get(k, 0) + int(v)
    counts = {
        "postings": db.scalar(conn, "SELECT COUNT(*) FROM postings"),
        "listed": db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE delisted_at IS NULL"),
        "with_description": db.scalar(
            conn, "SELECT COUNT(*) FROM postings WHERE description_fetched = 1"
        ),
        "applications": db.scalar(conn, "SELECT COUNT(*) FROM applications"),
        "sources_enabled": db.scalar(
            conn, "SELECT COUNT(*) FROM company_sources WHERE enabled = 1"
        ),
        "raw_payloads": db.scalar(conn, "SELECT COUNT(*) FROM raw_payloads"),
        "dead_links": db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE url_status = 'dead'"),
    }
    console.print("[bold]Job Radar health[/]")
    console.print("  " + " · ".join(f"{k}={v}" for k, v in counts.items()))
    if last_run:
        console.print(
            f"  last run: {last_run['kind']} {ago_human(last_run['started_at'])} status={last_run['status']}"
        )
    in_scope = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE in_scope = 1") or 0
    enriched = db.scalar(conn, "SELECT COUNT(*) FROM postings WHERE has_requirements = 1") or 0
    console.print(
        f"  LLM calls total: {llm_total}  per model: {per_model or '—'}  (enrichment {'enabled' if cfg.llm.enabled else 'DISABLED'}; models {cfg.llm.models()})"
    )
    if counts["postings"]:
        console.print(
            f"  two-stage funnel: {counts['postings']} postings → {in_scope} in scope ({in_scope / counts['postings']:.1%}) → {enriched} LLM-enriched ({enriched / counts['postings']:.2%} of all; rules eliminated {1 - in_scope / counts['postings']:.1%} before any model ran)"
        )
    console.print(
        f"  db: {cfg.db_path} ({cfg.db_path.stat().st_size / 1e6:.1f} MB)"
        if cfg.db_path.exists()
        else "  db missing"
    )
    lb = db.kv_get(conn, "last_backup_at")
    console.print(
        f"  last backup: {ago_human(lb) if lb else 'never'} · last cycle: {ago_human(db.kv_get(conn, 'last_cycle_at')) if db.kv_get(conn, 'last_cycle_at') else 'never'} · last snapshot: {ago_human(db.kv_get(conn, 'last_snapshot_at')) if db.kv_get(conn, 'last_snapshot_at') else 'never'}"
    )
    for w in warnings:
        console.print(f"  [yellow]![/] {w}")
    if problems:
        console.print(f"[red]{len(problems)} problem(s):[/]")
        for p in problems:
            console.print(f"  [red]✗[/] {p}")
        raise typer.Exit(1)
    console.print("[green]✓ healthy[/]")


@app.command()
def export(
    query: str = typer.Argument("", help="Query language filter (default: everything)"),
    out: Path = typer.Option(Path("data/export.csv"), "--out", "-o"),
    everything: bool = typer.Option(
        True, "--all/--default-view", help="Master view (default) or default-view suppressions"
    ),
) -> None:
    """Export postings to CSV. Every row carries apply_url + link status."""
    from radar.query import compile_query

    conn = _conn()
    q = compile_query(query)
    where = q.where if everything else f"({q.where}) AND {default_view_where()}"
    rows = db.all_rows(
        conn, f"SELECT p.* FROM postings p WHERE {where} ORDER BY p.first_seen_at DESC", q.params
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(out, rows)
    console.print(f"wrote {len(rows)} rows → {out}")


# ----------------------------------------------------------------------------------------------
# Phase 6 — autonomy: backups, launchd, nightly, snapshots, calibration, kits, public export
# ----------------------------------------------------------------------------------------------


@app.command()
def backup(
    dest: Path | None = typer.Option(None, "--dest", help="Directory (default paths.backups_dir)"),
) -> None:
    """Consistent online backup (gzipped), integrity-checked before it counts; prunes to 14 daily + 8 weekly."""
    from radar.ops.backup import backup as do_backup

    cfg = get_config()
    conn = _conn(cfg)
    info = do_backup(cfg, conn, dest_dir=dest)
    console.print(
        f"[green]✓[/] {info.path} ({info.bytes / 1e6:.1f} MB, {info.elapsed_s}s) integrity={info.integrity} "
        + " ".join(f"{k}={v}" for k, v in info.tables.items())
    )


@app.command()
def restore(
    path: Path = typer.Argument(..., help="A data/backups/radar-*.db.gz file"),
    force: bool = typer.Option(
        False, "--force", help="Allow rolling back applications or restoring an empty DB"
    ),
    verify_only: bool = typer.Option(
        False, "--verify-only", help="Decompress + integrity-check without touching the live DB"
    ),
) -> None:
    """Restore a backup over the live DB after verifying it; the previous DB is kept as radar.db.replaced-<ts>."""
    from radar.ops.backup import restore as do_restore
    from radar.ops.backup import verify_backup

    cfg = get_config()
    if verify_only:
        integrity, counts, out = verify_backup(path, cfg.data_dir / "restore-tmp")
        out.unlink(missing_ok=True)
        console.print(f"integrity={integrity} " + " ".join(f"{k}={v}" for k, v in counts.items()))
        raise typer.Exit(0 if integrity == "ok" else 1)
    r = do_restore(cfg, path, force=force)
    console.print(
        f"[green]✓ restored[/] from {r['restored_from']}; previous DB kept at {r['replaced']}; counts {r['counts']}"
    )


@app.command("registry-audit")
def registry_audit_cmd(
    apply: bool = typer.Option(
        False, "--apply", help="Disable the low-yield sources (report only without it)"
    ),
    also: list[str] = typer.Option(
        None, "--also", help="Company names to disable as broken_low_yield"
    ),
) -> None:
    """Disable sources whose whole history produced (almost) no in-scope rows and never fed the
    queue. Never touches tier-1/2/dream. Undo: nightly re-probe after 14 days (D62)."""
    from radar.ops.retention import registry_audit

    cfg = get_config()
    conn = _conn(cfg)
    r = registry_audit(conn, apply=apply, extra_disable=also)
    console.print(
        f"{'disabled' if apply else 'would disable'} {r['candidates']} sources carrying {r['postings_carried']} postings"
    )
    for line in r["disabled"][:25]:
        console.print(f"  · {line}")
    if not apply:
        console.print("(report only — rerun with --apply)")


@app.command("restore-workflow")
def restore_workflow(
    path: Path = typer.Argument(..., help="A data/backups/workflow-*.json.gz file"),
) -> None:
    """Re-apply your statuses, dismiss reasons, notes, applications and saved filters onto a rebuilt DB (by natural key). Never downgrades `applied`."""
    from radar.ops.backup import import_workflow

    cfg = get_config()
    console.print(import_workflow(_conn(cfg), path))


@app.command("install-launchd")
def install_launchd(
    port: int = typer.Option(8787, "--port"),
    interval: int = typer.Option(900, "--interval", help="Seconds between cycles"),
    print_only: bool = typer.Option(
        False, "--print", help="Print the plists / systemd units instead of installing"
    ),
) -> None:
    """Install the three launchd agents (cycle every 15 min with catch-up-on-wake, dashboard, nightly). macOS only; prints systemd units elsewhere."""
    import platform

    from radar.ops import launchd

    cfg = get_config()
    if print_only or platform.system() != "Darwin":
        if platform.system() == "Darwin":
            import plistlib

            for label, pl in launchd.plists(cfg, port=port, interval_s=interval).items():
                console.print(f"[bold]{label}[/]")
                console.print(plistlib.dumps(pl).decode())
        else:
            for name, body in launchd.systemd_units(cfg, port=port).items():
                console.print(f"[bold]~/.config/systemd/user/{name}[/]\n{body}")
            console.print(
                "then: systemctl --user daemon-reload && systemctl --user enable --now jobradar-cycle.timer jobradar-nightly.timer jobradar-serve.service"
            )
        return
    written = launchd.install(cfg, port=port, interval_s=interval)
    for w in written:
        console.print(f"[green]✓[/] {w}")
    console.print(
        "Agents loaded. Missed intervals (sleep) fire once on wake → one consolidated catch-up summary. "
        f"Logs: {cfg.data_dir / 'logs'}. Status: `radar launchd-status`. Remove: `radar uninstall-launchd`."
    )


@app.command("uninstall-launchd")
def uninstall_launchd() -> None:
    """Unload and delete the launchd agents."""
    from radar.ops import launchd

    for r in launchd.uninstall():
        console.print(f"removed {r}")


@app.command("launchd-status")
def launchd_status() -> None:
    """Show whether the three agents are loaded/running."""
    from radar.ops import launchd

    for label, st in launchd.status().items():
        console.print(f"{label:<24} {st}")


@app.command()
def nightly(
    skip_backup: bool = typer.Option(False, "--skip-backup"),
) -> None:
    """The 03:30 job: backup → weekly velocity snapshot (Mondays or if stale) → monthly calibration (1st or if stale) → alarms."""
    from radar.ops import alarms
    from radar.ops.backup import backup as do_backup
    from radar.ops.backup import export_workflow
    from radar.ops.calibrate import run_calibration
    from radar.ops.launchd import single_instance
    from radar.ops.snapshot import take_snapshot
    from radar.scheduler import _hours_since

    cfg = get_config()
    conn = _conn(cfg)
    with single_instance(cfg, "nightly") as mine:
        if not mine:
            console.print("another nightly is running; skipping")
            return
        run_id = db.start_run(conn, "nightly")
        stats: dict[str, Any] = {}
        try:
            wf = export_workflow(conn, cfg.backups_dir)
            stats["workflow_export"] = str(wf)
            console.print(f"workflow export → {wf}")
            if not skip_backup:
                info = do_backup(cfg, conn)
                stats["backup"] = {
                    "path": str(info.path),
                    "mb": round(info.bytes / 1e6, 1),
                    "integrity": info.integrity,
                }
                console.print(f"backup → {info.path} ({info.bytes / 1e6:.1f} MB)")
            from radar.ops.retention import (
                prune_out_of_scope_descriptions,
                prune_raw_payloads,
                registry_audit,
                reprobe_disabled,
            )

            n = prune_out_of_scope_descriptions(conn)
            pr = prune_raw_payloads(conn, cfg)
            stats["retention"] = {"descriptions_cleared": n, "raw": pr}
            console.print(f"retention: {n} out-of-scope descriptions cleared · raw prune {pr}")
            h = _hours_since(conn, "last_snapshot_at")
            if h is None or h >= 6.5 * 24:
                stats["snapshot"] = take_snapshot(conn)
                console.print(f"snapshot → {stats['snapshot']}")
                back = reprobe_disabled(conn)
                aud = registry_audit(conn, apply=True)
                stats["registry"] = {"reprobed": back, "disabled": aud["candidates"]}
                console.print(
                    f"registry: re-probed {back} low-yield sources · re-disabled {aud['candidates']}"
                )
            h = _hours_since(conn, "last_calibration_at")
            if h is None or h >= 27 * 24:
                r = run_calibration(conn, cfg)
                stats["calibration"] = {
                    "labeled": r["labeled"],
                    "enough_signal": r["enough_signal"],
                    "path": r["path"],
                }
                console.print(
                    f"calibration → {r['path']} (labeled {r['labeled']}, enough signal: {r['enough_signal']})"
                )
            al = alarms.evaluate(conn, cfg)
            stats["alarms"] = alarms.as_dicts(al)
            pushed = alarms.push(conn, cfg, al)
            for a in al:
                console.print(
                    f"[{'red' if a.severity == 'error' else 'yellow'}]{a.severity}[/] {a.title}: {a.detail}"
                )
            console.print(f"alarms: {len(al)} ({pushed} pushed)")
            db.finish_run(conn, run_id, stats=stats)
        except Exception as e:
            db.finish_run(conn, run_id, status="failed", error=str(e))
            raise


@app.command()
def snapshot() -> None:
    """Weekly velocity snapshot: learned time-to-close per company, hiring velocity, repost rate, market row."""
    from radar.ops.snapshot import take_snapshot

    cfg = get_config()
    r = take_snapshot(_conn(cfg))
    console.print(r)


@app.command()
def calibrate(
    out: Path | None = typer.Option(
        None, "--out", help="Default: CALIBRATION.md in the project root (git-ignored)"
    ),
) -> None:
    """Monthly calibration: fit revealed preference → propose weights in CALIBRATION.md. Never auto-applied."""
    from radar.ops.calibrate import run_calibration

    cfg = get_config()
    r = run_calibration(_conn(cfg), cfg, out_path=out)
    console.print(
        f"wrote {r['path']} — labeled {r['labeled']} (positives {r['positives']}); proposal: {r['proposed_weights'] or 'not enough signal yet'}"
    )


@app.command()
def kit(
    posting_id: int = typer.Argument(...),
    force: bool = typer.Option(False, "--force", help="Redraft even if a kit exists"),
    print_kit: bool = typer.Option(True, "--print/--no-print"),
) -> None:
    """Draft the application kit for one posting (drafting model): bullets order, why-this-firm, referral note, interview themes. Never sent."""
    from radar.enrich.llm import LLMUnavailable
    from radar.ops.kits import build_kit

    cfg = get_config()
    try:
        r = build_kit(_conn(cfg), cfg, posting_id, force=force)
    except LLMUnavailable as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    console.print(
        f"{'cached' if r['cached'] else 'drafted'} kit for #{posting_id}"
        + (f" → {r['path']}" if r.get("path") else "")
    )
    if print_kit:
        from rich.markdown import Markdown

        console.print(Markdown(r["kit_md"]))


@app.command("export-public")
def export_public_cmd(
    dest: Path = typer.Argument(..., help="New, empty directory for the sanitized copy"),
    extra_term: list[str] = typer.Option(None, "--redact", help="Additional strings to redact"),
) -> None:
    """Sanitized public export (§17.7): copy of the repo with placeholders for every personal value; refuses if anything survives."""
    from radar.ops.public_export import export_public

    cfg = get_config()
    r = export_public(cfg, dest, extra_terms=extra_term)
    console.print(
        f"exported {r['files']} files → {r['dest']} (checked {r['forbidden_terms_checked']} forbidden terms)"
    )
    if r["remaining_hits"]:
        console.print(f"[red]personal data survived in binary files:[/] {r['remaining_hits']}")
        raise typer.Exit(1)
    console.print(
        "[green]✓ clean[/] — `cd` there, `git init`, and review config/config.yaml before pushing anywhere"
    )


if __name__ == "__main__":
    app()
