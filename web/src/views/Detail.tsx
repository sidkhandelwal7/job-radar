/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { api, type PostingDetail } from '../lib/api'
import { ACTION_LABEL, category, day, linkStatus, metro, money, pct, signedMoney } from '../lib/format'
import { ApplyLink, LinkBadge, ScoreBar, Verdict } from '../components/PostingBits'
import { BASELINE, DetailWaterfall, fromPosting, isPosted } from '../components/CompWaterfall'
import { DeadLink, ErrorState } from '../components/States'

/** Detail view (§13): description, score decomposition with evidence, beats-baseline breakdown, requirement checklist, cluster siblings, timeline, notes, the verified apply link — prominent. */
export default function Detail({ id, onBack }: { id: number; onBack: () => void }) {
  const [p, setP] = useState<PostingDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [notes, setNotes] = useState('')
  const [verifying, setVerifying] = useState(false)
  const [kit, setKit] = useState<{ kit_md: string | null; kit_at: string | null } | null>(null)
  const [kitBusy, setKitBusy] = useState(false)
  const [kitErr, setKitErr] = useState<string | null>(null)
  useEffect(() => {
    api.kit(id).then((k) => setKit(k.kit_md ? k : null)).catch(() => setKit(null))
  }, [id])
  const draftKit = async (force: boolean) => {
    setKitBusy(true)
    setKitErr(null)
    try {
      const r = await api.draftKit(id, force)
      setKit(r)
    } catch (e: any) {
      setKitErr(String(e.message ?? e))
    } finally {
      setKitBusy(false)
    }
  }
  const load = useCallback(() => {
    api
      .posting(id)
      .then((d) => {
        setP(d)
        setNotes(d.notes_md ?? '')
      })
      .catch((e) => setErr(String(e.message ?? e)))
  }, [id])
  useEffect(load, [load])
  useEffect(() => {
    // §14: verify on view when older than 48h
    if (p && p.url_last_verified_at && Date.now() - Date.parse(p.url_last_verified_at) > 48 * 3600 * 1000 && !verifying) {
      setVerifying(true)
      api.verify(p.id).then(load).finally(() => setVerifying(false))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p?.id])

  if (err)
    return (
      <div className="p-4">
        <ErrorState message={err} retry={load} />
      </div>
    )
  if (!p) return <div className="p-4 muted small">Loading…</div>
  const x = p.score_explanation ?? {}
  const loc = x.location
  const sub = x.sub_scores ?? {}
  const act = async (body: Record<string, unknown>) => {
    const r = await api.action(p.id, body)
    if (r.duplicate && window.confirm(`Duplicate guard: ${r.duplicate.reason}. Record anyway?`)) await api.action(p.id, { ...body, force: true })
    load()
  }

  const link = linkStatus(p)
  const posted = isPosted(p.comp_source, p.base_posted_min, p.base_posted_max)
  const delta = p.effective_value != null ? p.effective_value - BASELINE : null
  return (
    <div className="mx-auto max-w-5xl space-y-4 p-3 sm:p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <button className="btn btn-sm" onClick={onBack}>
          ← back
        </button>
        <h1 className="text-[20px] leading-7">
          {p.is_dream_list ? <span title="dream list" style={{ color: 'var(--better)' }}>★ </span> : null}
          {p.company_name} <span className="quiet font-normal">— {p.title}</span>
        </h1>
        <span className="num muted">#{p.id}</span>
      </div>

      {/* THE link, prominent; a dead link explains itself */}
      {link.dead && <DeadLink since={p.url_last_verified_at} siblings={p.siblings} onOpenSibling={(id) => (window.location.hash = `#/posting?id=${id}`)} />}
      <div className="card flex flex-wrap items-center gap-3">
        <ApplyLink p={p} big />
        <div className="min-w-0 grow">
          <div className="mono caption muted truncate" title={p.apply_url}>
            {p.apply_url}
          </div>
          <LinkBadge p={p} /> {verifying && <span className="muted caption">re-verifying…</span>}
          <button className="ml-2 caption underline" style={{ color: 'var(--better)' }} onClick={() => { setVerifying(true); api.verify(p.id).then(load).finally(() => setVerifying(false)) }}>
            verify now
          </button>
          {p.canonical_url && p.canonical_url !== p.apply_url && (
            <div className="caption muted truncate">
              canonical: <a href={p.canonical_url} target="_blank" rel="noreferrer">{p.canonical_url}</a>
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-1">
          <button className="btn btn-primary" onClick={() => act({ action: 'applied' })}>✓ Mark applied</button>
          <button className="btn" onClick={() => act({ action: p.status === 'shortlisted' ? 'unshortlist' : 'shortlist' })}>{p.status === 'shortlisted' ? '★ shortlisted' : '☆ shortlist'}</button>
          <button className="btn" onClick={() => { const c = window.prompt('Who is referring you?'); if (c) act({ action: 'referral', contact: c }) }}>{p.referral_secured ? '✓ referral logged' : '+ referral secured'}</button>
          <button className="btn" onClick={() => act({ action: 'snooze', days: 7 })}>snooze 7d</button>
          <button className="btn btn-danger" onClick={() => { const r = window.prompt('Dismiss — why?'); if (r !== null) act({ action: 'dismiss', reason: r }) }}>dismiss…</button>
        </div>
      </div>
      {p.duplicate_warning && (
        <div className="state state-alert small">
          <b>Duplicate guard:</b> {p.duplicate_warning.reason} — {p.duplicate_warning.applications.map((a: any) => `#${a.id} ${a.company_name} (${a.stage}, ${day(a.applied_at)})`).join(', ')}
        </div>
      )}

      {/* The signature: how this role measures against the baseline offer */}
      <section className="card">
        <div className="grid gap-4 md:grid-cols-[220px_1fr]">
          <div>
            <div className="caption muted uppercase tracking-wider">Effective value vs baseline</div>
            {p.effective_value != null ? (
              <>
                <div className="figure">{money(p.effective_value, false)}</div>
                <div className="num" style={{ color: delta != null && delta >= 0 ? 'var(--better)' : 'var(--worse)' }}>
                  {delta != null ? `${delta >= 0 ? '+' : '−'}$${Math.abs(Math.round(delta)).toLocaleString()}` : ''} <span className="muted">vs baseline</span>
                </div>
              </>
            ) : (
              <div className="figure muted">—</div>
            )}
            <div className="mt-2">
              <Verdict v={p.beats_baseline} />
            </div>
            <div className="small quiet mt-1">{x.verdict?.reason}{x.verdict?.confidence ? <span className="muted"> · confidence {x.verdict.confidence}</span> : null}</div>
            {x.comp && (
              <div className="caption muted mt-2">
                {posted ? null : <><span className="est num">~estimate · {Math.round((p.comp_confidence ?? 0) * 100)}% confidence</span> · </>}
                {x.comp.explanation}
                {x.comp.tc_year1_est ? ` Year-1 TC ≈ ${money(x.comp.tc_year1_est)} (signing ${money(x.comp.signing_est)} × 50%, ${x.comp.equity_type} ${money(x.comp.equity_annual_est)}).` : ''}
              </div>
            )}
            {loc && (
              <div className="caption muted mt-1">
                real terms vs baseline (tax + purchasing power, no premium): <span className="num whitespace-nowrap">{signedMoney(loc.real_terms_vs_baseline)}</span> — informational, never drives ranking
              </div>
            )}
            {p.same_market_as_baseline_offer ? <div className="chip mt-2" style={{ borderColor: 'var(--arguable)', color: 'var(--arguable)' }}>⚑ same recruiting market as the baseline offer — you decide</div> : null}
          </div>
          <div className="min-w-0 overflow-x-auto">
            <DetailWaterfall d={fromPosting(p)} realTerms={loc?.real_terms_vs_baseline} metroName={metro(p.primary_metro)} taxLabel={loc ? `${loc.tax_jurisdiction} ${(loc.tax_rate * 100).toFixed(1)}% vs baseline ${(loc.baseline_tax_rate * 100).toFixed(1)}%` : undefined} />
          </div>
        </div>
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Score */}
        <section className="card space-y-1">
          <h2 className="mb-1">
            Score {p.composite_score?.toFixed(3)} {p.apply_priority_rank ? <span className="muted small font-normal">· queue #{p.apply_priority_rank} · {ACTION_LABEL[p.queue_action ?? ''] ?? p.queue_action}</span> : <span className="muted small font-normal">· not in queue{p.scope_reason ? ` — ${p.scope_reason}` : ''}</span>}
          </h2>
          {Object.entries(sub).map(([k, v]: [string, any]) => (
            <ScoreBar key={k} label={k.replace('_score', '')} value={v.value} weight={v.weight} why={v.why} />
          ))}
          {x.modifiers?.length ? <div className="caption muted">modifiers: {x.modifiers.join(', ')}</div> : null}
          {x.urgency && (
            <div className="small">
              urgency <b>{x.urgency.value.toFixed(2)}</b> <span className="muted caption">— {x.urgency.days_open} days open, {Math.round(x.urgency.estimated_days_to_close) > 0 ? `est. ${Math.round(x.urgency.estimated_days_to_close)} days to close` : 'already past the typical lifetime — could close any day'} (company median {x.urgency.median_days_to_close}{x.urgency.median_days_to_close === 45 ? ' default' : ' learned'}){x.urgency.first_drop ? ', first drop of the season' : ''}{x.urgency.deadline_proximity ? `, deadline proximity ${x.urgency.deadline_proximity}` : ''}</span>
            </div>
          )}
          {x.ev?.p_offer !== undefined && (
            <div className="small">
              EV <b>{money(p.ev_estimate, false)}</b> <span className="muted caption">= P(offer) {pct(x.ev.p_offer)} × (3-yr delta {money(x.ev.three_year_effective_delta, false)} + career premium {money(x.ev.career_capital_premium, false)}) − prep {money(x.ev.prep_cost, false)} − P(offer) × switching friction {money(x.ev.switching_friction_if_offer, false)}</span>
            </div>
          )}
          <details className="caption muted">
            <summary>every input (JSON)</summary>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify({ sub_scores: sub, urgency: x.urgency, ev: x.ev, switching_friction_today: x.switching_friction_today, scope: x.scope }, null, 1)}</pre>
          </details>
        </section>

        {/* Application kit (§11): drafts only, never sent */}
        <section className="card">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2>Application kit <span className="muted caption font-normal">drafts only — you send everything yourself</span></h2>
            <button className="btn" disabled={kitBusy} onClick={() => draftKit(!!kit)}>{kitBusy ? 'drafting…' : kit ? 'Redraft' : 'Draft kit'}</button>
          </div>
          {kitErr && <div className="small" style={{ color: 'var(--alert)' }}>■ {kitErr}</div>}
          {kit ? (
            <details className="mt-2 small" open>
              <summary className="muted caption">drafted {kit.kit_at?.slice(0, 16)}Z · also saved to data/kits/{p.id}.md</summary>
              <pre className="mt-2 max-h-[32rem] overflow-auto whitespace-pre-wrap font-[family-name:var(--font-body)]">{kit.kit_md}</pre>
            </details>
          ) : (
            <div className="muted small">Resume bullets re-ordered for this posting, a why-this-firm first draft grounded in the posting, a referral note to paste, and the three likeliest interview themes. One pinned model call, cached.</div>
          )}
        </section>

        {/* Requirements vs resume */}
        <section className="card">
          <h2 className="mb-2">Requirements vs your resume</h2>
          {p.requirement_checklist.length ? (
            <ul className="space-y-0.5 small">
              {p.requirement_checklist.map((c, i) => (
                <li key={i} className="flex gap-2">
                  <span className="num" style={{ color: c.status === 'have' ? 'var(--better)' : c.status === 'gap' ? 'var(--alert)' : c.status === 'partial' ? 'var(--arguable)' : 'var(--ink-50)' }}>{c.status === 'have' ? '✓' : c.status === 'gap' ? '✗' : c.status === 'partial' ? '◐' : '?'}</span>
                  <span>{c.item}</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted small">Requirements not extracted yet (the classifier model runs on the top of the queue each cycle; run <code>radar enrich</code> to do it now).</div>
          )}
          {p.requirements?.summary && <div className="mt-2 caption quiet">{p.requirements.summary}</div>}
          <div className="mt-2 grid grid-cols-2 gap-x-4 caption quiet">
            <div>clearance: {p.requires_clearance === 1 ? <b style={{ color: 'var(--alert)' }}>required</b> : p.requires_clearance === 0 ? 'no' : '?'}</div>
            <div>MS/PhD: {p.requires_advanced_degree === 1 ? <b style={{ color: 'var(--alert)' }}>required</b> : p.requires_advanced_degree === 0 ? 'no' : '?'}</div>
            <div>min years: {p.min_years_experience ?? '?'}</div>
            <div>sponsorship: {p.sponsorship ?? '?'}</div>
            <div>grad window: {p.graduation_window ?? '?'}</div>
            <div>employment: {p.employment_type}</div>
          </div>
          {(p.hard_blockers?.length || p.floor_fail_reasons?.length) ? <div className="mt-2 small" style={{ color: 'var(--alert)' }}>■ Suppressed: {[...(p.hard_blockers ?? []), ...(p.floor_fail_reasons ?? [])].join('; ')} {p.floor_result === 'fail' && <button className="ml-1 underline" onClick={() => act({ action: 'override_floor' })}>override floor for this posting</button>}</div> : null}
        </section>

        {/* Fit */}
        <section className="card">
          <h2 className="mb-2">Fit <span className="num">{p.fit_score?.toFixed(2)}</span> <span className="muted small font-normal">· loop: {p.prep_archetype} · ~{p.prep_hours_est} prep hours · referral {p.referral_likelihood}{p.referral_secured ? ' ✓ secured' : ''}</span></h2>
          {p.matched_strengths?.length ? (
            <div className="mb-2">
              <div className="caption font-medium uppercase tracking-wider" style={{ color: 'var(--better)' }}>Matched strengths (with resume evidence)</div>
              <ul className="small">
                {p.matched_strengths.map((m, i) => (
                  <li key={i}>
                    ✓ {m.strength}
                    {m.evidence && <div className="ml-4 caption muted">“{m.evidence}”</div>}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {p.gaps?.length ? (
            <div>
              <div className="caption font-medium uppercase tracking-wider" style={{ color: 'var(--arguable)' }}>Gaps</div>
              <ul className="small">
                {p.gaps.map((g, i) => (
                  <li key={i}>
                    <span style={{ color: g.severity === 'high' ? 'var(--alert)' : 'var(--arguable)' }}>{g.severity === 'high' ? '✗' : '◐'}</span> {g.gap}
                    {g.note && <span className="muted caption"> — {g.note}</span>}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {p.requirements?.llm_fit?.interview_themes && (
            <div className="mt-2 small">
              <div className="caption font-medium uppercase tracking-wider muted">Likely interview themes</div>
              <ol className="list-decimal pl-5">
                {p.requirements.llm_fit.interview_themes.map((t: string, i: number) => (
                  <li key={i}>{t}</li>
                ))}
              </ol>
              <div className="muted caption">{p.requirements.llm_fit.one_line_verdict}</div>
            </div>
          )}
        </section>
      </div>

      {/* facts + siblings + timeline + notes */}
      <div className="grid gap-4 md:grid-cols-3">
        <section className="card small">
          <h2 className="mb-1">Facts</h2>
          <div>{metro(p.primary_metro)} · {p.work_mode} · {category(p.target_category)}{p.company_tier ? ` · Tier ${p.company_tier}` : ''}</div>
          <div className="muted caption">{(p.locations ?? []).map((l) => l.raw).join(' · ')}</div>
          <div>{p.role_family}/{p.role_subfamily} · {p.seniority}{p.is_stretch ? ' (stretch)' : ''} · {p.program_type}</div>
          <div>posted {day(p.posted_at)} · first seen {day(p.first_seen_at)} · last seen {p.last_seen_at?.slice(0, 16)}{p.delisted_at ? ` · DELISTED ${day(p.delisted_at)}` : ''}{p.application_deadline ? ` · deadline ${day(p.application_deadline)}` : ''}</div>
          <div className="muted caption">source {p.source_provider} ({p.source}) · tags {p.tech_tags?.join(', ') || '—'}{p.repost_of_id ? ` · repost of #${p.repost_of_id}` : ''}</div>
          {p.applications?.length ? <div className="mt-1" style={{ color: 'var(--better)' }}>Applied: {p.applications.map((a: any) => `#${a.id} ${a.stage} (${day(a.applied_at)})`).join(', ')}</div> : null}
        </section>
        <section className="card small">
          <h2 className="mb-1">Same posting via other sources ({p.siblings.length})</h2>
          {p.siblings.length ? (
            <ul>
              {p.siblings.map((s: any) => (
                <li key={s.id} className="truncate">
                  {s.is_cluster_canonical ? '★ ' : ''}
                  <span className="muted">{s.source_provider}</span> <a href={s.apply_url} target="_blank" rel="noreferrer">{s.title}</a> <span className="muted caption">({s.url_status})</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted">none — only this source lists it</div>
          )}
          {!p.is_cluster_canonical && <div className="muted caption">This row is a sibling; the ★ row is canonical (company-direct preferred).</div>}
        </section>
        <section className="card small">
          <h2 className="mb-1">Timeline</h2>
          <ul className="max-h-40 overflow-auto caption">
            {p.events.map((e: any, i: number) => (
              <li key={i}>
                <span className="num muted">{e.at.slice(0, 16)}</span> {e.type}
                {e.data?.reason ? ` — ${e.data.reason}` : ''}
                {e.data?.to ? ` → ${e.data.to}` : ''}
              </li>
            ))}
          </ul>
        </section>
      </div>

      <section className="card">
        <h2 className="mb-1">Notes</h2>
        <textarea className="w-full" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} onBlur={() => notes !== (p.notes_md ?? '') && act({ action: 'note', text: notes })} placeholder="Your notes (saved when you click away)" />
      </section>

      <section className="card">
        <h2 className="mb-1">Description</h2>
        {p.description_md ? <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap font-[family-name:var(--font-body)] small">{p.description_md}</pre> : <div className="muted small">No description stored (detail not fetched for this title — <code>radar fetch --detail all_new --company …</code> fetches it).</div>}
      </section>
    </div>
  )
}
