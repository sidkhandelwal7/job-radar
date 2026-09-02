/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { money } from '../lib/format'

/** Config view (§13): edit weights/premiums/gates, PREVIEW the ranking impact, then commit + rescore. */
export default function ConfigView() {
  const [cfg, setCfg] = useState<any>(null)
  const [draft, setDraft] = useState<any>(null)
  const [preview, setPreview] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    api.config().then((c) => {
      setCfg(c)
      setDraft(JSON.parse(JSON.stringify(c)))
    })
  }, [])
  if (!cfg || !draft) return <div className="p-4 muted small">Loading…</div>
  const changes = (): Record<string, unknown> => {
    const out: Record<string, unknown> = {}
    for (const k of ['weights', 'location_utility_premium', 'comp_gates', 'modifiers', 'switching_friction', 'throughput', 'ev']) {
      if (JSON.stringify(draft[k]) !== JSON.stringify(cfg[k])) out[k] = draft[k]
    }
    return out
  }
  const wsum = Object.values(draft.weights as Record<string, number>).reduce((a, b) => a + Number(b), 0)
  const run = async (commit: boolean) => {
    setBusy(true)
    setErr(null)
    try {
      const r = await api.previewConfig(changes(), commit)
      setPreview(r)
      if (commit) {
        const c = await api.config()
        setCfg(c)
        setDraft(JSON.parse(JSON.stringify(c)))
      }
    } catch (e: any) {
      setErr(String(e.message ?? e))
    } finally {
      setBusy(false)
    }
  }
  const Num = ({ path, step = 1000, min, max }: { path: [string, string]; step?: number; min?: number; max?: number }) => (
    <input
      type="number"
      step={step}
      min={min}
      max={max}
      className="num w-28 text-right"
      value={draft[path[0]][path[1]]}
      onChange={(e) => setDraft({ ...draft, [path[0]]: { ...draft[path[0]], [path[1]]: Number(e.target.value) } })}
    />
  )
  return (
    <div className="mx-auto max-w-5xl p-3 sm:p-4 space-y-4">
      <div className="card space-y-1 small">
        <h2>Plain-language check</h2>
        {(preview?.plain_language ?? cfg.plain_language).map((s: string, i: number) => (
          <div key={i}>• {s}</div>
        ))}
        <div className="muted caption">If you disagree, change the number below, preview, and commit. Baseline: {cfg.baseline.employer} {money(cfg.baseline.base_salary, false)} base + {money(cfg.baseline.signing_bonus, false)} signing in {cfg.baseline.metro}, decision by {cfg.baseline.decision_deadline}.</div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <section className="card">
          <h2>
            Weights <span className="small font-normal num" style={{ color: Math.abs(wsum - 1) > 1e-6 ? 'var(--alert)' : 'var(--ink-50)' }}>sum {wsum.toFixed(2)} — must be 1.00</span>
          </h2>
          {Object.keys(draft.weights).map((k) => (
            <label key={k} className="flex items-center gap-2 small">
              <span className="w-40 capitalize">{k.replace(/_/g, ' ')}</span>
              <input type="range" min={0} max={0.5} step={0.01} value={draft.weights[k]} onChange={(e) => setDraft({ ...draft, weights: { ...draft.weights, [k]: Number(e.target.value) } })} className="grow" />
              <span className="num w-10">{Number(draft.weights[k]).toFixed(2)}</span>
            </label>
          ))}
        </section>
        <section className="card small">
          <h2>Location premiums ($/yr, added back after COL)</h2>
          {['new_york', 'san_francisco', 'seattle', 'other_major_tech_hub', 'washington_dc', 'remote', 'elsewhere'].map((k) => (
            <label key={k} className="flex items-center justify-between gap-2">
              <span className="capitalize">{k.replace(/_/g, ' ')}</span>
              <Num path={['location_utility_premium', k]} />
            </label>
          ))}
          <label className="flex items-center justify-between gap-2 muted">
            <span>COL uplift cap (share of base)</span>
            <Num path={['location_utility_premium', 'col_uplift_cap']} step={0.05} min={0} max={5} />
          </label>
        </section>
        <section className="card small">
          <h2>Comp gates (base $)</h2>
          {['instant_yes', 'parity', 'hard_floor'].map((k) => (
            <label key={k} className="flex items-center justify-between gap-2">
              <span className="capitalize">{k.replace(/_/g, ' ')}</span>
              <Num path={['comp_gates', k]} />
            </label>
          ))}
          <label className="flex items-center justify-between gap-2 muted">
            <span>instant-yes needs confidence ≥</span>
            <Num path={['comp_gates', 'instant_yes_requires_confidence']} step={0.05} min={0} max={1} />
          </label>
        </section>
        <section className="card small">
          <h2>Switching friction (itemized; zero any term)</h2>
          {['signing_bonus_clawback', 'goodwill_cost_at_signing', 'goodwill_cost_at_start', 'university_channel_cost', 'same_market_penalty'].map((k) => (
            <label key={k} className="flex items-center justify-between gap-2">
              <span className="capitalize">{k.replace(/_/g, ' ')}</span>
              <Num path={['switching_friction', k]} step={500} />
            </label>
          ))}
          <label className="flex items-center justify-between gap-2">
            <span>curve exponent (1 linear, 2 convex)</span>
            <Num path={['switching_friction', 'curve_exponent']} step={0.5} min={1} max={4} />
          </label>
        </section>
        <section className="card small">
          <h2>Throughput & EV</h2>
          <label className="flex items-center justify-between gap-2"><span>Applications / week</span><Num path={['throughput', 'applications_per_week']} step={1} min={1} /></label>
          <label className="flex items-center justify-between gap-2"><span>Prep hours / week</span><Num path={['throughput', 'prep_hours_per_week']} step={1} min={0} /></label>
          <label className="flex items-center justify-between gap-2"><span>Today bucket cap</span><Num path={['throughput', 'today_bucket_max']} step={1} min={1} max={50} /></label>
          <label className="flex items-center justify-between gap-2"><span>Hourly opportunity cost ($)</span><Num path={['ev', 'hourly_opportunity_cost']} step={5} /></label>
        </section>
        <section className="card small">
          <h2>Modifiers</h2>
          {Object.keys(draft.modifiers).map((k) => (
            <label key={k} className="flex items-center justify-between gap-2">
              <span className="capitalize">{k.replace(/_/g, ' ')}</span>
              <Num path={['modifiers', k]} step={0.05} min={0} max={2} />
            </label>
          ))}
          <div className="muted caption mt-1">Dream list, blocked lists, and LLM models are edited in <code>config/config.yaml</code> (models are pinned to {Object.values(cfg.llm.models).join('/')}; LLM {cfg.llm.enabled ? 'enabled' : 'disabled'}).</div>
        </section>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button className="btn" disabled={busy || !Object.keys(changes()).length || Math.abs(wsum - 1) > 1e-6} onClick={() => run(false)}>
          Preview ranking impact
        </button>
        <button className="btn btn-primary" disabled={busy || !preview || preview.committed || Math.abs(wsum - 1) > 1e-6} onClick={() => window.confirm('Write config.yaml and rescore everything?') && run(true)}>
          Commit & rescore
        </button>
        <button className="btn" onClick={() => { setDraft(JSON.parse(JSON.stringify(cfg))); setPreview(null) }}>
          Reset
        </button>
        {busy && <span className="muted small">working…</span>}
        {err && <span className="small" style={{ color: 'var(--alert)' }}>■ {err}</span>}
      </div>

      {preview && (
        <section className="card">
          <h2>{preview.committed ? 'Committed.' : 'Preview'} — top 30 before → after {preview.note && <span className="muted small font-normal">({preview.note})</span>}</h2>
          <div className="grid gap-2 md:grid-cols-2 small">
            {(['before', 'after'] as const).map((side) => (
              <ol key={side} className="list-decimal pl-6">
                <div className="font-medium capitalize">{side}</div>
                {preview[side].map((r: any) => {
                  const other = preview[side === 'before' ? 'after' : 'before'].find((x: any) => x.id === r.id)
                  const moved = other ? other.apply_priority_rank - r.apply_priority_rank : null
                  return (
                    <li key={r.id} style={!other ? { color: 'var(--arguable)' } : undefined}>
                      {r.company_name} — {r.title.slice(0, 48)} <span className="muted">({r.beats_baseline}, {Number(r.composite_score).toFixed(2)}{side === 'before' && moved !== null && moved !== 0 ? `, → #${other.apply_priority_rank}` : ''})</span>
                    </li>
                  )
                })}
              </ol>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
