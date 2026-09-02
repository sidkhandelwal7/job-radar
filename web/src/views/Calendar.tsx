/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { day, money } from '../lib/format'

/** Decision calendar (§11): two dates, not one. */
export default function Calendar({ onOpenPosting }: { onOpenPosting: (id: number) => void }) {
  const [cal, setCal] = useState<any>(null)
  useEffect(() => {
    api.calendar().then(setCal)
  }, [])
  if (!cal) return <div className="p-4 muted small">Loading…</div>
  const curve: any[] = cal.switching_window.curve
  const max = Math.max(...curve.map((c) => c.total))
  return (
    <div className="mx-auto max-w-5xl space-y-4 p-3 sm:p-4">
      <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
        <div>
          <div className="caption muted uppercase tracking-wider">Baseline decision deadline</div>
          <div className="figure" style={{ color: cal.days_to_deadline <= 14 ? 'var(--alert)' : undefined }}>
            {cal.days_to_deadline} <span className="text-[16px] quiet font-[family-name:var(--font-body)] tracking-normal">days</span>
          </div>
          <div className="num small muted">{cal.baseline_decision_deadline}</div>
        </div>
        <p className="small quiet max-w-xl pb-1">{cal.deadline_note}</p>
      </div>

      <section className="card">
        <h2>
          Rolling switching window <span className="num muted font-normal">{cal.switching_window.from} → {cal.switching_window.to}</span>
        </h2>
        <p className="small muted">Cost of walking away from the baseline offer by date, itemized from <code>config.switching_friction</code> (zero any term you disagree with). Cheap zone ends {cal.switching_window.cheap_zone_ends}.</p>
        <div className="mt-2 space-y-1">
          {curve.map((c) => (
            <div key={c.date} className="flex items-center gap-2 caption num">
              <span className="w-24">{c.date}</span>
              <div className="h-2.5 grow rounded-sm" style={{ background: 'var(--ink-06)' }}>
                <div className="h-2.5 rounded-sm" style={{ width: `${(c.total / max) * 100}%`, background: c.cheap_zone ? 'var(--ink-25)' : 'var(--arguable)' }} title={`clawback ${money(c.items.signing_bonus_clawback, false)} · goodwill ${money(c.items.goodwill, false)} · university ${money(c.items.university_channel, false)}`} />
              </div>
              <span className="w-20 text-right">{money(c.total, false)}</span>
              <span className="w-14" style={{ color: c.cheap_zone ? 'var(--ink-50)' : 'var(--arguable)' }}>{c.cheap_zone ? 'cheap' : '◆ rising'}</span>
            </div>
          ))}
        </div>
        <p className="mt-2 small">
          Walking away from the baseline today would cost about <b>{money(cal.switching_window.today.total, false)}</b> (clawback {money(cal.switching_window.today.items.signing_bonus_clawback, false)}, goodwill {money(cal.switching_window.today.items.goodwill, false)}, university channel {money(cal.switching_window.today.items.university_channel, false)}).
        </p>
      </section>

      <section className="card">
        <h2>
          Season <span className="num muted font-normal">{cal.season.start} → {cal.season.end} · {Math.round(cal.season.elapsed_fraction * 100)}% elapsed</span>
        </h2>
        <div className="mt-2 h-2.5 w-full rounded-sm" style={{ background: 'var(--ink-06)' }}>
          <div className="h-2.5 rounded-sm" style={{ width: `${cal.season.elapsed_fraction * 100}%`, background: 'var(--ink)' }} />
        </div>
        <p className="mt-2 small quiet">{cal.season.note}</p>
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <section className="card">
          <h2>Upcoming stated deadlines <span className="muted small font-normal">queue only</span></h2>
          {cal.upcoming_deadlines.length ? (
            <ul className="small mt-1">
              {cal.upcoming_deadlines.map((d: any) => (
                <li key={d.id} className="flex gap-2">
                  <span className="num w-24 muted">{day(d.application_deadline)}</span>
                  <button className="text-left hover:underline" onClick={() => onOpenPosting(d.id)}>
                    {d.company_name} — {d.title}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <div className="state small mt-1"><span className="quiet">Few postings state deadlines; urgency uses each company's learned time-to-close instead.</span></div>
          )}
        </section>
        <section className="card">
          <h2>Follow-ups due</h2>
          {cal.follow_ups.length ? (
            <ul className="small mt-1">
              {cal.follow_ups.map((f: any) => (
                <li key={f.id}>
                  <span className="num muted">{day(f.follow_up_due)}</span> #{f.id} {f.company_name} — {f.title}
                </li>
              ))}
            </ul>
          ) : (
            <div className="state small mt-1"><span className="quiet">None due. Follow-ups come due 10 business days after applying with no response.</span></div>
          )}
        </section>
      </div>
    </div>
  )
}
