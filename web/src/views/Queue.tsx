import { useCallback, useEffect, useState } from 'react'
import { api, type Posting } from '../lib/api'
import { ACTION_LABEL, metro, money, num } from '../lib/format'
import { setHash } from '../lib/url'
import { ActionTag, ApplyLink, Base, LinkBadge, Verdict, Where } from '../components/PostingBits'
import { RowWaterfall, fromPosting } from '../components/CompWaterfall'
import { ErrorState } from '../components/States'

/** The Apply-First Queue (§11): what's left to do, best first. j/k move, s shortlist, a applied, x dismiss, Enter opens. */
export default function Queue({ onOpen }: { onOpen: (id: number) => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.queue>> | null>(null)
  const [action, setAction] = useState<string>('')
  const [cursor, setCursor] = useState(0)
  const [busy, setBusy] = useState<number | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(() => {
    api.queue(action || undefined).then(setData).catch((e) => setErr(String(e.message ?? e)))
  }, [action])
  useEffect(load, [load])

  const rows = data?.rows ?? []
  const doAction = async (p: Posting, body: Record<string, unknown>) => {
    setBusy(p.id)
    try {
      const r = await api.action(p.id, body)
      if (r.duplicate) {
        const d = r.duplicate
        if (window.confirm(`Duplicate guard: ${d.reason}\n\n${d.applications.map((a: { company_name: string; title: string; stage: string }) => `• ${a.company_name} — ${a.title} (${a.stage})`).join('\n')}\n\nRecord it anyway?`)) {
          await api.action(p.id, { ...body, force: true })
        }
      }
      load()
    } finally {
      setBusy(null)
    }
  }
  const markApplied = (p: Posting) => doAction(p, { action: 'applied' })
  const dismiss = (p: Posting) => {
    const reason = window.prompt(`Dismiss "${p.title}" at ${p.company_name}. Why? (feeds calibration)`, '')
    if (reason !== null) doAction(p, { action: 'dismiss', reason })
  }
  const shortlist = (p: Posting) => doAction(p, { action: p.status === 'shortlisted' ? 'unshortlist' : 'shortlist' })
  const snooze = (p: Posting) => doAction(p, { action: 'snooze', days: 7 })

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === 'INPUT' || (e.target as HTMLElement)?.tagName === 'TEXTAREA') return
      const p = rows[cursor]
      if (e.key === 'j') setCursor((c) => Math.min(rows.length - 1, c + 1))
      else if (e.key === 'k') setCursor((c) => Math.max(0, c - 1))
      else if (e.key === 'Enter' && p) onOpen(p.id)
      else if (e.key === 'o' && p) window.open(p.apply_url, '_blank')
      else if (e.key === 's' && p) shortlist(p)
      else if (e.key === 'a' && p) markApplied(p)
      else if (e.key === 'x' && p) dismiss(p)
      else if (e.key === 'z' && p) snooze(p)
      else return
      e.preventDefault()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })

  if (err)
    return (
      <div className="p-4">
        <ErrorState message={err} retry={load} />
      </div>
    )
  if (!data) return <div className="p-4 muted small">Loading the queue…</div>
  const today = rows.filter((r) => r.queue_action === 'apply_today')
  const rest = rows.filter((r) => r.queue_action !== 'apply_today' && r.queue_action !== 'verify_link')
  const dead = !action || action === 'verify_link' ? (data.dead ?? []) : []
  const restore = (p: Posting) => doAction(p, { action: 'restore_link' })

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-3 sm:p-4">
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
        <div>
          <div className="caption muted uppercase tracking-wider">Left to act on</div>
          <div className="figure">{data.total.toLocaleString()}</div>
        </div>
        <div className="small quiet pb-1.5">
          today's bucket caps at <span className="num">{data.today_cap}</span> · you said ~<span className="num">{data.applications_per_week}</span> applications a week
        </div>
        <span className="grow" />
        <select value={action} onChange={(e) => setAction(e.target.value)} aria-label="Filter by action">
          <option value="">All actions</option>
          {Object.entries(ACTION_LABEL).map(([k, v]) => (
            <option key={k} value={k}>
              {v} ({data.counts[k] ?? 0})
            </option>
          ))}
        </select>
        <button className="btn" onClick={() => setHash('table', { q: 'rank >= 1', sort: 'rank' })}>
          Open as table
        </button>
      </div>
      <p className="caption muted hidden sm:block">
        <span className="kbd">j</span>/<span className="kbd">k</span> move · <span className="kbd">Enter</span> details · <span className="kbd">o</span> open link · <span className="kbd">a</span> mark applied · <span className="kbd">s</span> shortlist · <span className="kbd">x</span> dismiss ·{' '}
        <span className="kbd">z</span> snooze 7d. Marking applied creates the application record and removes the row from this queue.
      </p>

      {today.length > 0 && (
        <section>
          <h2 className="mb-2">
            Today <span className="num muted font-normal">{today.length}</span>
          </h2>
          <div className="space-y-2">
            {today.map((p) => (
              <Card key={p.id} p={p} active={rows[cursor]?.id === p.id} busy={busy === p.id} onOpen={onOpen} onApplied={markApplied} onDismiss={dismiss} onShortlist={shortlist} onSnooze={snooze} />
            ))}
          </div>
        </section>
      )}
      {dead.length > 0 && (
        <section aria-label="dead links">
          <h2 className="mb-1" style={{ color: 'var(--alert)' }}>
            ■ Link dead — verify before dismissing <span className="num muted font-normal">{dead.length}</span>
          </h2>
          <p className="caption muted mb-2">
            These would be in the queue, but the employer's page stopped serving the req (404, redirect to a generic page, or “no longer available”). Open it: if it is still up, <b>still live, restore</b> puts it back; if not, dismiss it with the reason “closed”. Unverified links (JS-rendered boards) are not here — they keep their place above with their marker.
          </p>
          <ul className="card !p-0 divide-y" style={{ borderColor: 'var(--ink-12)' }}>
            {dead.map((p) => (
              <li key={p.id} className={`flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 ${busy === p.id ? 'opacity-60' : ''}`} style={{ borderColor: 'var(--ink-06)' }}>
                <span className="num muted w-7 text-right">{p.apply_priority_rank}</span>
                <button className="min-w-0 grow text-left hover:underline" onClick={() => onOpen(p.id)}>
                  <span className="font-medium">{p.company_name}</span> <span className="quiet">— {p.title}</span>
                </button>
                <LinkBadge p={p} />
                <ApplyLink p={p} />
                <button className="btn btn-sm" onClick={() => restore(p)} title="Re-verify; if the verifier still says dead, your word wins and it returns to the queue">
                  still live, restore
                </button>
                <button className="btn btn-sm btn-danger" onClick={() => doAction(p, { action: 'dismiss', reason: 'closed' })} title="Dismiss with the reason “closed”">
                  closed, dismiss
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section>
        <h2 className="mb-2">
          {today.length ? 'Next' : 'Queue'} <span className="num muted font-normal">{rest.length}{rest.length >= 50 ? '+' : ''}</span>
        </h2>
        <div className="space-y-2">
          {rest.map((p) => (
            <Card key={p.id} p={p} active={rows[cursor]?.id === p.id} busy={busy === p.id} onOpen={onOpen} onApplied={markApplied} onDismiss={dismiss} onShortlist={shortlist} onSnooze={snooze} />
          ))}
        </div>
        {rows.length === 0 && (
          <div className="state small">
            <div className="font-medium">Nothing left to act on{action ? ` under “${ACTION_LABEL[action] ?? action}”` : ''}.</div>
            <div className="quiet mt-1">
              {action ? 'Clear the action filter to see the rest of the queue. ' : 'Every scored, in-scope, floor-passing posting has been applied to, dismissed, or snoozed. '}
              New postings arrive with each 15-minute cycle; the Table view shows everything, including what the filters hide.
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

function Card({ p, active, busy, onOpen, onApplied, onDismiss, onShortlist, onSnooze }: { p: Posting; active: boolean; busy: boolean; onOpen: (id: number) => void; onApplied: (p: Posting) => void; onDismiss: (p: Posting) => void; onShortlist: (p: Posting) => void; onSnooze: (p: Posting) => void }) {
  return (
    <div className={`card ${busy ? 'opacity-60' : ''}`} style={active ? { boxShadow: 'inset 3px 0 0 var(--better)' } : undefined} aria-current={active ? 'true' : undefined}>
      <div className="grid gap-x-3 gap-y-2 md:grid-cols-[28px_minmax(0,1fr)_auto]">
        <div className="num hidden pt-0.5 text-right text-[15px] muted md:block">{p.apply_priority_rank}</div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <button className="text-left font-medium hover:underline" onClick={() => onOpen(p.id)}>
              <span className="num muted mr-1 md:hidden">{p.apply_priority_rank}</span>
              {p.is_dream_list ? <span title="dream list" style={{ color: 'var(--better)' }}>★ </span> : null}
              {p.company_name} <span className="quiet font-normal">— {p.title}</span>
            </button>
            <ActionTag a={p.queue_action} />
            {p.same_market_as_baseline_offer ? <span className="chip" style={{ borderColor: 'var(--arguable)', color: 'var(--arguable)' }} title="same recruiting market as the baseline offer">⚑ same market as baseline</span> : null}
            {p.is_stretch ? <span className="chip">1–2 YoE stretch</span> : null}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 small">
            <span className="inline-flex items-center gap-2" title="nominal base → tax vs baseline → purchasing power → location premium = effective value, against the baseline rule">
              <RowWaterfall d={fromPosting(p)} width={120} height={18} />
              <Verdict v={p.beats_baseline} reason={p.beats_baseline_reason} />
            </span>
            <span className="quiet">
              base <Base p={p} />
            </span>
            <span className="quiet" title="after-tax, purchasing-power-adjusted base in baseline-metro pre-tax dollars + your location premium">
              effective <span className="num" style={{ color: 'var(--ink)' }}>{money(p.effective_value)}</span>
            </span>
            <span className="quiet">
              <Where p={p} />
            </span>
            <span className="muted caption num">
              score {num(p.composite_score)} · urg {num(p.urgency_score)} · fit {num(p.fit_score)}
            </span>
          </div>
          {p.beats_baseline_reason && <div className="caption muted mt-0.5 line-clamp-1">{p.beats_baseline_reason}</div>}
        </div>
        <div className="flex flex-wrap items-start gap-1 md:justify-end">
          <ApplyLink p={p} big />
          <button className="btn" onClick={() => onApplied(p)} title="Creates the application record and removes this from the queue (a)">
            ✓ Applied
          </button>
          <button className="btn" onClick={() => onShortlist(p)} title="Shortlist (s)" aria-label="shortlist">
            {p.status === 'shortlisted' ? '★' : '☆'}
          </button>
          <button className="btn" onClick={() => onSnooze(p)} title="Snooze 7 days (z)" aria-label="snooze 7 days">
            zz
          </button>
          <button className="btn btn-danger" onClick={() => onDismiss(p)} title="Dismiss with a reason (x)" aria-label="dismiss">
            ✕
          </button>
        </div>
      </div>
      <div className="caption muted mt-1.5 flex flex-wrap gap-x-3">
        <LinkBadge p={p} />
        <span>
          {metro(p.primary_metro)} · {p.source_provider} · first seen {p.first_seen_age}
          {p.application_deadline ? ` · deadline ${p.application_deadline.slice(0, 10)}` : ''}
        </span>
      </div>
    </div>
  )
}
