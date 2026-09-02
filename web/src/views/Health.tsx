/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { ago, pct } from '../lib/format'

export default function Health() {
  const [h, setH] = useState<any>(null)
  const [ops, setOps] = useState<any>(null)
  useEffect(() => {
    api.health().then(setH)
    api.ops().then(setOps).catch(() => setOps(null))
  }, [])
  if (!h) return <div className="p-4 muted small">Loading…</div>
  const c = h.counts
  return (
    <div className="mx-auto max-w-6xl p-3 sm:p-4 space-y-4">
      {ops && (
        <section className="card small" style={ops.alarms?.length ? { borderColor: 'var(--alert)' } : undefined}>
          <h2>Autonomy {ops.alarms?.length ? <span style={{ color: 'var(--alert)' }}>· ■ {ops.alarms.length} alarm(s)</span> : <span className="muted font-normal">· no alarms</span>}</h2>
          {ops.alarms?.map((a: any) => (
            <div key={a.key} style={{ color: a.severity === 'error' ? 'var(--alert)' : 'var(--arguable)' }}>
              <b>{a.title}</b> — {a.detail}
            </div>
          ))}
          <div className="mt-1 grid gap-x-6 gap-y-0.5 sm:grid-cols-2 lg:grid-cols-4 muted">
            {Object.entries(ops.last ?? {}).map(([k, v]) => (
              <div key={k}>{k.replace('last_', '').replace(/_at$/, '').replace(/_/g, ' ')}: <span className="text-[var(--fg)]">{v ? ago(String(v)) : 'never'}</span></div>
            ))}
            {Object.entries(ops.launchd ?? {}).map(([k, v]) => (
              <div key={k}>{k.replace('com.jobradar.', 'launchd ')}: <span className="text-[var(--fg)]">{String(v)}</span></div>
            ))}
          </div>
          {ops.backups?.length ? <div className="muted mt-1">backups: {ops.backups.map((b: any) => `${b.name} (${b.mb} MB)`).join(' · ')}</div> : <div className="muted mt-1">no backups yet — <code>radar backup</code> or <code>radar install-launchd</code></div>}
          {ops.calibration && <div className="muted">last calibration: {ops.calibration.month} — {ops.calibration.labeled} labeled ({ops.calibration.positives} kept) → CALIBRATION.md</div>}
          {ops.market_weeks?.length ? (
            <div className="muted mt-1">market by week: {ops.market_weeks.slice(0, 6).map((w: any) => `${w.week_start}: +${w.new_reqs} −${w.closed_reqs} (${w.in_scope_open} in scope open${w.median_days_to_close ? `, ttc ${w.median_days_to_close}d` : ''})`).join(' · ')}</div>
          ) : null}
        </section>
      )}
      <div className="grid grid-cols-2 gap-2 small sm:grid-cols-4 lg:grid-cols-8">
        {Object.entries(c).map(([k, v]) => (
          <div key={k} className="card">
            <div className="caption muted">{k.replace(/_/g, ' ')}</div>
            <div className="num text-[16px]">{Number(v).toLocaleString()}</div>
          </div>
        ))}
      </div>
      <div className="card small">
        <b>Two-stage funnel:</b> {c.postings.toLocaleString()} postings → {h.funnel.in_scope.toLocaleString()} in scope → {h.funnel.llm_enriched.toLocaleString()} LLM-enriched. Rules eliminated <b>{pct(h.funnel.rules_eliminated_share, 1)}</b> before any model ran.
        <div className="muted">LLM {h.llm.enabled ? 'enabled' : 'DISABLED'} · models {JSON.stringify(h.llm.models)} · calls total {h.llm.calls_total} · per model {JSON.stringify(h.llm.per_model)} · last cycle {ago(h.last_cycle_at)} · Actions invocations this month: {h.actions_invocations_this_month} (each bills ≥ 1 min; real minutes are on GitHub's billing page)</div>
      </div>
      <section className="card">
        <h2>Recent runs</h2>
        <table className="dense w-full">
          <thead><tr><th>id</th><th>kind</th><th>started</th><th>status</th><th>LLM calls</th><th>error</th></tr></thead>
          <tbody>
            {h.runs.map((r: any) => (
              <tr key={r.id}><td>{r.id}</td><td>{r.kind}</td><td>{ago(r.started_at)}</td><td className={r.status === 'failed' ? '' : ''}>{r.status}</td><td>{r.llm_calls} {r.llm_models_json !== '{}' ? r.llm_models_json : ''}</td><td className="muted">{r.error ?? ''}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="card">
        <h2>Sources (failing / stalest first)</h2>
        <div className="max-h-[28rem] overflow-auto">
          <table className="dense w-full">
            <thead><tr><th>company</th><th>provider</th><th>cadence</th><th>last ok</th><th>rows</th><th>typical</th><th>fails</th><th>error</th></tr></thead>
            <tbody>
              {h.sources.map((s: any, i: number) => (
                <tr key={i} className={s.consecutive_failures >= 3 ? '' : ''}><td>{s.name}</td><td>{s.provider}</td><td>{s.cadence}</td><td>{ago(s.last_success_at)}</td><td>{s.last_row_count}</td><td>{s.typical_row_count ? Math.round(s.typical_row_count) : ''}</td><td>{s.consecutive_failures || ''}</td><td className="muted truncate max-w-xs">{s.last_error ?? ''}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
