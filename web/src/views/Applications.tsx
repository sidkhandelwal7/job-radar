/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { category, day, pct } from '../lib/format'
import { copy } from '../components/PostingBits'
import { ErrorState } from '../components/States'

const STAGES = ['applied', 'oa_pending', 'oa_done', 'screen', 'onsite', 'offer', 'rejected', 'ghosted', 'withdrawn']
/** The five nodes of the track. oa_pending/oa_done collapse onto one "OA" node (pending = half). */
const TRACK: { key: string; label: string; matches: string[] }[] = [
  { key: 'applied', label: 'applied', matches: ['applied'] },
  { key: 'oa', label: 'OA', matches: ['oa_pending', 'oa_done'] },
  { key: 'screen', label: 'screen', matches: ['screen'] },
  { key: 'onsite', label: 'onsite', matches: ['onsite'] },
  { key: 'offer', label: 'offer', matches: ['offer'] },
]
const TERMINAL: Record<string, { label: string; color: string }> = {
  rejected: { label: 'rejected', color: 'var(--alert)' },
  ghosted: { label: 'ghosted', color: 'var(--worse)' },
  withdrawn: { label: 'withdrawn', color: 'var(--worse)' },
}
const trackIndex = (stage: string) => TRACK.findIndex((t) => t.matches.includes(stage))

/** Where an application is, drawn as a track: reached nodes filled, current node ringed, the rest
 *  hollow; a terminal outcome hangs off the last node it reached. Shape + fill, never color alone. */
function StageTrack({ stage, reachedIndexHint }: { stage: string; reachedIndexHint?: number }) {
  const terminal = TERMINAL[stage]
  const cur = terminal ? (reachedIndexHint ?? 0) : trackIndex(stage)
  const half = stage === 'oa_pending'
  const w = 200
  const x0 = 26
  const step = (w - 2 * x0) / (TRACK.length - 1)
  return (
    <svg width={w} height={34} viewBox={`0 0 ${w} 34`} role="img" aria-label={`stage: ${stage}`} style={{ display: 'block' }}>
      <line x1={x0} x2={x0 + step * (TRACK.length - 1)} y1={11} y2={11} stroke="var(--ink-12)" strokeWidth="1.5" />
      {cur > 0 && <line x1={x0} x2={x0 + step * cur} y1={11} y2={11} stroke={terminal ? terminal.color : 'var(--ink)'} strokeWidth="1.5" />}
      {TRACK.map((t, i) => {
        const cx = x0 + step * i
        const reached = i < cur
        const isCur = i === cur && !terminal
        const fill = reached ? 'var(--ink)' : isCur ? (half ? 'var(--surface)' : 'var(--ink)') : 'var(--surface)'
        return (
          <g key={t.key}>
            {isCur && <circle cx={cx} cy={11} r={7} fill="none" stroke="var(--ink)" strokeWidth="1.5" />}
            <circle cx={cx} cy={11} r={reached || isCur ? 4 : 3} fill={fill} stroke={reached || isCur ? 'var(--ink)' : 'var(--ink-25)'} strokeWidth="1.25" />
            {isCur && half && <path d={`M${cx},7 A4,4 0 0 0 ${cx},15 Z`} fill="var(--ink)" />}
            <text x={cx} y={29} textAnchor="middle" fontSize="9.5" fontFamily="var(--font-body)" fill={isCur ? 'var(--ink)' : 'var(--ink-50)'} fontWeight={isCur ? 600 : 400}>
              {t.label}
            </text>
          </g>
        )
      })}
      {terminal && (
        <g>
          <rect x={x0 + step * cur - 4} y={7} width={8} height={8} fill={terminal.color} transform={`rotate(45 ${x0 + step * cur} 11)`} />
          <text x={Math.min(w - 2, x0 + step * cur + 11)} y={14} fontSize="9.5" fontFamily="var(--font-body)" fontWeight={600} fill={terminal.color} textAnchor={cur >= TRACK.length - 1 ? 'end' : 'start'}>
            {terminal.label}
          </text>
        </g>
      )}
    </svg>
  )
}

const daysSince = (iso: string | null | undefined) => (iso ? Math.floor((Date.now() - Date.parse(iso)) / 86400000) : null)

/** Applications (§15): the permanent record, read like a ledger, not worked like a queue. */
export default function Applications({ onOpenPosting }: { onOpenPosting: (id: number) => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.applications>> | null>(null)
  const [showCompleted, setShowCompleted] = useState(false)
  const [form, setForm] = useState({ url: '', company_name: '', title: '', location: '', applied_at: '', stage: 'applied', notes: '', referral_contact: '', source_of_discovery: 'manual' })
  const [autofillNote, setAutofillNote] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [flash, setFlash] = useState<number | null>(null)
  const load = useCallback(() => api.applications().then(setData).catch((e) => setErr(String(e.message ?? e))), [])
  useEffect(() => {
    load()
  }, [load])

  if (err)
    return (
      <div className="p-4">
        <ErrorState message={err} retry={load} />
      </div>
    )
  if (!data) return <div className="p-4 muted small">Loading…</div>
  const rows = data.rows.filter((r) => showCompleted || !r.completed).slice().sort((a: any, b: any) => String(b.stage_changed_at ?? b.applied_at).localeCompare(String(a.stage_changed_at ?? a.applied_at)))
  const st = data.stats
  const dueIds = new Set(data.follow_ups_due.map((a: any) => a.id))
  const ghostIds = new Set(data.ghosted_candidates.map((a: any) => a.id))

  const autofill = async () => {
    if (!form.url) return
    setAutofillNote('looking up…')
    const af = await api.autofill(form.url)
    setForm((f) => ({ ...f, company_name: f.company_name || af.company_name || '', title: f.title || af.title || '', location: f.location || af.location || '' }))
    setAutofillNote(af.note ? af.note : `autofilled via ${af.source} (confidence ${Math.round((af.confidence ?? 0) * 100)}%)${af.posting_id ? ` — matches posting #${af.posting_id}` : ''}`)
  }
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const body: Record<string, unknown> = { ...form, applied_at: form.applied_at || null, url: form.url || null }
    let r = await api.createApplication(body)
    if (r.duplicate && window.confirm(`Duplicate guard: ${r.duplicate.reason}. Record anyway?`)) r = await api.createApplication({ ...body, force: true })
    if (r.ok) {
      setForm({ url: '', company_name: '', title: '', location: '', applied_at: '', stage: 'applied', notes: '', referral_contact: '', source_of_discovery: 'manual' })
      setAutofillNote(null)
      load()
    }
  }
  const setStage = async (id: number, stage: string) => {
    const note = stage === 'offer' ? window.prompt('Base offered? (optional, number)') : null
    await api.patchApplication(id, { stage, base_offered: note ? Number(note) : undefined })
    setFlash(id)
    setTimeout(() => setFlash(null), 500)
    load()
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-3 sm:p-4">
      {/* ledger header: the numbers that describe the season so far */}
      <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
        <div>
          <div className="caption muted uppercase tracking-wider">Applications</div>
          <div className="figure">{st.total ?? 0}</div>
        </div>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-0.5 small sm:grid-cols-3 lg:flex lg:gap-x-8">
          <Ledger label="active" value={st.active} />
          <Ledger label="response rate" value={pct(st.response_rate)} />
          <Ledger label="median days to 1st response" value={st.median_days_to_first_response ? Math.round(st.median_days_to_first_response) : '—'} />
          <Ledger label="follow-ups due" value={data.follow_ups_due.length} tone={data.follow_ups_due.length ? 'var(--arguable)' : undefined} />
          <Ledger label="likely ghosted (30d+)" value={data.ghosted_candidates.length} />
          <Ledger label="completed" value={st.completed} />
        </dl>
      </div>
      <div className="caption muted -mt-3">
        by week {Object.entries(st.by_week ?? {}).map(([w, n]) => `${w} ${n}`).join(' · ') || '—'} &nbsp;·&nbsp; by category {Object.entries(st.by_category ?? {}).map(([k, n]) => `${category(k)} ${n}`).join(' · ') || '—'} &nbsp;·&nbsp; found via {Object.entries(st.by_source ?? {}).map(([k, n]) => `${k} ${n}`).join(' · ') || '—'}
      </div>

      {/* the only part of this page that asks something of you */}
      {(data.follow_ups_due.length > 0 || data.ghosted_candidates.length > 0) && (
        <div className="state small" style={{ borderColor: data.follow_ups_due.length ? 'var(--arguable)' : 'var(--ink-25)', borderStyle: 'solid' }}>
          {data.follow_ups_due.length > 0 && (
            <div>
              <b style={{ color: 'var(--arguable)' }}>◆ Follow up</b> <span className="quiet">— 10 business days since applying, no response:</span>{' '}
              {data.follow_ups_due.map((a: any, i: number) => (
                <span key={a.id}>
                  {i > 0 && ' · '}
                  <b>{a.company_name}</b> <span className="num muted">{day(a.follow_up_due)}</span>
                </span>
              ))}
            </div>
          )}
          {data.ghosted_candidates.length > 0 && (
            <div className={data.follow_ups_due.length ? 'mt-1' : ''}>
              <b className="quiet">▽ 30+ days, nothing back</b> <span className="quiet">— mark ghosted when you're ready (suggested, never automatic):</span>{' '}
              {data.ghosted_candidates.map((a: any, i: number) => (
                <span key={a.id}>
                  {i > 0 && ' · '}
                  <b>{a.company_name}</b>
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* the record */}
      <section>
        <div className="mb-1 flex flex-wrap items-center gap-3 small">
          <h2>Record</h2>
          <label className="flex items-center gap-1.5 quiet">
            <input type="checkbox" checked={showCompleted} onChange={(e) => setShowCompleted(e.target.checked)} /> show completed ({st.completed})
          </label>
          <a className="btn btn-sm no-underline" href="/api/applications/export.csv">
            Export CSV
          </a>
          <span className="caption muted">Permanent: nothing here is ever deleted or hidden by a posting filter.</span>
        </div>
        {rows.length === 0 ? (
          <div className="state small">
            <div className="font-medium">No applications recorded{showCompleted ? '' : ' that are still active'}.</div>
            <div className="quiet mt-1">
              Mark one applied from the Queue or Table (press <span className="kbd">a</span> on a row), or record one the system never saw below.
              {!showCompleted && st.completed ? <> {st.completed} completed record{st.completed === 1 ? '' : 's'} are hidden — tick “show completed”.</> : null}
            </div>
          </div>
        ) : (
          <ul className="card !p-0 divide-y" style={{ borderColor: 'var(--ink-12)' }}>
            {rows.map((a: any) => {
              const since = daysSince(a.applied_at)
              const due = dueIds.has(a.id)
              const ghost = ghostIds.has(a.id)
              return (
                <li key={a.id} className={`grid gap-x-4 gap-y-1 px-3 py-2.5 sm:grid-cols-[minmax(0,1fr)_200px_104px_auto] sm:items-center ${a.completed ? 'opacity-60' : ''} ${flash === a.id ? 'flash' : ''}`} style={{ borderColor: 'var(--ink-06)' }}>
                  <div className="min-w-0">
                    <div className="font-medium truncate">{a.company_name}</div>
                    <div className="quiet small truncate">
                      {a.posting_id ? (
                        <button className="truncate max-w-full text-left hover:underline" onClick={() => onOpenPosting(a.posting_id)}>
                          {a.title}
                        </button>
                      ) : (
                        a.title
                      )}
                    </div>
                    <div className="caption muted flex flex-nowrap items-center gap-x-2 overflow-hidden">
                      <span className="num">#{a.id}</span>
                      {a.location && <span className="truncate max-w-[32ch]" title={a.location}>{a.location}</span>}
                      {a.referral_used ? <span className="whitespace-nowrap" style={{ color: 'var(--better)' }}>✓ referral{a.referral_contact ? ` · ${a.referral_contact}` : ''}</span> : null}
                      {a.source_of_discovery && a.source_of_discovery !== 'radar' ? <span className="whitespace-nowrap">via {a.source_of_discovery}</span> : null}
                      {a.created_manually ? <span className="chip" style={{ height: 16 }}>manual</span> : null}
                      {a.base_offered ? <span className="num" style={{ color: 'var(--better)' }}>offer ${Number(a.base_offered).toLocaleString()}</span> : null}
                      {a.notes_md ? <span className="truncate max-w-[40ch] whitespace-nowrap" title={a.notes_md}>“{a.notes_md}”</span> : null}
                    </div>
                  </div>
                  <div>
                    <StageTrack stage={a.stage} />
                  </div>
                  <div className="text-left sm:text-right">
                      <div className="num">{day(a.applied_at)}</div>
                      <div className="caption" style={{ color: due ? 'var(--arguable)' : ghost ? 'var(--worse)' : 'var(--ink-50)' }}>
                        {since === null ? '' : since === 0 ? 'today' : `${since}d ago`}
                        {due ? ' · follow up' : ghost ? ' · no reply' : a.follow_up_due && !a.completed && a.stage === 'applied' ? ` · nudge ${day(a.follow_up_due).slice(5)}` : ''}
                      </div>
                  </div>
                  <div className="flex items-center gap-x-2 sm:justify-end">
                    <select className="!py-0.5 small" value={a.stage} onChange={(e) => setStage(a.id, e.target.value)} aria-label={`stage for ${a.company_name}`}>
                      {STAGES.map((x) => (
                        <option key={x}>{x}</option>
                      ))}
                    </select>
                    <span className="inline-flex w-[84px] items-center gap-1 whitespace-nowrap small">
                      {a.apply_url ? (
                        <>
                          <a href={a.apply_url} target="_blank" rel="noreferrer">
                            open ↗
                          </a>
                          <button className="btn btn-sm !px-1.5" onClick={() => copy(a.apply_url)} title="copy link">
                            ⧉
                          </button>
                        </>
                      ) : (
                        <span className="muted">no link</span>
                      )}
                    </span>
                    <button className="btn btn-sm" onClick={() => api.patchApplication(a.id, { completed: !a.completed }).then(load)} title="'I'm done acting on this' — leaves the active list, stays in the record">
                      {a.completed ? 'reopen' : 'done'}
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {/* manual entry lives at the bottom: it's the exception, not the job of this page */}
      <details className="card">
        <summary className="small font-medium">Record an application the system never saw</summary>
        <form className="mt-3 grid gap-2 sm:grid-cols-6" onSubmit={submit}>
          <input className="sm:col-span-4" placeholder="Posting URL (autofills company / title / location when it can)" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} onBlur={autofill} aria-label="Posting URL" />
          <button type="button" className="btn sm:col-span-2" onClick={autofill}>
            Autofill from URL
          </button>
          <input placeholder="Company *" required value={form.company_name} onChange={(e) => setForm({ ...form, company_name: e.target.value })} className="sm:col-span-2" aria-label="Company" />
          <input placeholder="Title *" required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className="sm:col-span-2" aria-label="Title" />
          <input placeholder="Location" value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })} className="sm:col-span-2" aria-label="Location" />
          <input type="date" value={form.applied_at} onChange={(e) => setForm({ ...form, applied_at: e.target.value })} title="Applied on (default today)" aria-label="Applied on" />
          <select value={form.stage} onChange={(e) => setForm({ ...form, stage: e.target.value })} aria-label="Stage">
            {STAGES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <input placeholder="Found via (simplify, friend, …)" value={form.source_of_discovery} onChange={(e) => setForm({ ...form, source_of_discovery: e.target.value })} aria-label="Found via" />
          <input placeholder="Referral contact" value={form.referral_contact} onChange={(e) => setForm({ ...form, referral_contact: e.target.value })} aria-label="Referral contact" />
          <input placeholder="Notes" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} className="sm:col-span-2" aria-label="Notes" />
          <button className="btn btn-primary justify-center sm:col-span-6" type="submit">
            Record application
          </button>
        </form>
        {autofillNote && <div className="caption muted mt-1">{autofillNote}</div>}
      </details>
    </div>
  )
}

const Ledger = ({ label, value, tone }: { label: string; value: any; tone?: string }) => (
  <div className="flex items-baseline gap-2 lg:block">
    <dt className="caption muted order-2 lg:order-1">{label}</dt>
    <dd className="num order-1 text-[15px] lg:order-2" style={{ color: tone }}>
      {value ?? '—'}
    </dd>
  </div>
)
