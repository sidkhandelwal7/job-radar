/**
 * CompWaterfall — the one bold thing on the page.
 *
 * Nominal base → tax vs the baseline jurisdiction → purchasing power → location premium → effective
 * value, drawn left-to-right against a FIXED dollar scale so the baseline rule sits at the same x in
 * every row: in a table the rules stack into one continuous line and you read "which side" at a
 * glance. The decision zone (floor · parity · instant-yes) is ~23 px wide at row size, which is the
 * whole point of the tight scale.
 *
 * Confidence is texture: a posted range is a solid bar with its min–max core; an inferred estimate
 * is hatched and prefixed "~" with its confidence printed. A row with no comp at all draws only the
 * rule and a dotted track — never a zero-width bar pretending to be a measurement.
 */
import { useId } from 'react'

export interface WaterfallInput {
  nominal: number | null | undefined
  col_adjusted: number | null | undefined
  premium: number | null | undefined // location utility premium (added back)
  tax_delta: number | null | undefined // positive = you pay MORE tax than in the baseline jurisdiction (subtracted)
  effective: number | null | undefined
  posted_min?: number | null
  posted_max?: number | null
  comp_source?: string | null
  confidence?: number | null
  verdict?: string | null
  scored?: boolean // false = not scored yet (pending), distinct from "no comp"
  out_of_scope?: string | null // scope reason when the row was light-scored (no comp estimate by design)
}

/** The baseline and gates come from /api/config at startup (App.tsx → setBaseline). The scale is
 *  derived from the baseline so its rule sits about one fifth in from the left at any salary. */
export let SCALE_MIN = 70_000
export let SCALE_MAX = 170_000
export let BASELINE = 90_000
export let FLOOR = 80_000
export let INSTANT_YES = 100_000
export function setBaseline(base: number, floor: number, instantYes: number) {
  BASELINE = base
  FLOOR = floor
  INSTANT_YES = instantYes
  SCALE_MIN = Math.round((base * 0.78) / 5000) * 5000
  SCALE_MAX = Math.round((base * 1.9) / 5000) * 5000
}
export const axisTicks = (): number[] => {
  const out: number[] = []
  for (let v = Math.ceil(SCALE_MIN / 25_000) * 25_000; v <= SCALE_MAX; v += 25_000) out.push(v)
  return out
}

const POSTED = new Set(['posted_range', 'posted_range_text', 'ashby_posted', 'greenhouse_posted', 'lever_posted', 'workday_posted'])
export const isPosted = (src: string | null | undefined, min?: number | null, max?: number | null) => !!(min || max) || (!!src && POSTED.has(src))

const k = (v: number) => `$${Math.round(v / 1000)}k`
const signedK = (v: number) => `${v < 0 ? '−' : '+'}$${Math.abs(Math.round(v / 1000))}k`

type Step = { key: string; label: string; from: number; to: number; delta: number }

/** Unit-consistent order (D56): after-tax first, then purchasing power, then the premium — every
 *  step is in baseline-metro pre-tax-equivalent dollars, so the baseline rule applies to the result. */
export function steps(d: WaterfallInput): Step[] | null {
  if (d.nominal == null || d.effective == null) return null
  const nominal = d.nominal
  const tax = d.tax_delta ?? 0 // positive = you keep less than in the baseline jurisdiction
  const afterTax = nominal - tax
  const col = d.col_adjusted ?? afterTax // after tax AND purchasing power
  const prem = d.premium ?? 0
  const out: Step[] = [
    { key: 'tax', label: 'tax vs baseline', from: nominal, to: afterTax, delta: -tax },
    { key: 'col', label: 'purchasing power', from: afterTax, to: col, delta: col - afterTax },
    { key: 'prem', label: 'location premium', from: col, to: col + prem, delta: prem },
  ]
  return out
}

export const verdictShape = (v: string | null | undefined) => (v === 'clearly_better' ? 'up' : v === 'arguably_better' ? 'diamond' : v === 'worse' ? 'down' : 'none')
const verdictColor = (v: string | null | undefined) => (v === 'clearly_better' ? 'var(--better)' : v === 'arguably_better' ? 'var(--arguable)' : v === 'worse' ? 'var(--worse)' : 'var(--ink-50)')

/** Verdict marker: filled ▲ / half ◆ / hollow ▽ — shape carries the meaning, hue reinforces it. */
export const Marker = ({ v, cx, cy, r = 4.5 }: { v: string | null | undefined; cx: number; cy: number; r?: number }) => {
  const c = verdictColor(v)
  const s = verdictShape(v)
  if (s === 'up') return <path d={`M${cx},${cy - r} L${cx + r},${cy + r * 0.8} L${cx - r},${cy + r * 0.8} Z`} fill={c} />
  if (s === 'diamond')
    return (
      <g>
        <path d={`M${cx},${cy - r} L${cx + r},${cy} L${cx},${cy + r} L${cx - r},${cy} Z`} fill="none" stroke={c} strokeWidth="1.4" />
        <path d={`M${cx},${cy - r} L${cx},${cy + r} L${cx - r},${cy} Z`} fill={c} />
      </g>
    )
  if (s === 'down') return <path d={`M${cx},${cy + r} L${cx + r},${cy - r * 0.8} L${cx - r},${cy - r * 0.8} Z`} fill="none" stroke={c} strokeWidth="1.4" />
  return <circle cx={cx} cy={cy} r={r * 0.7} fill="none" stroke={c} strokeWidth="1.2" strokeDasharray="2 1.5" />
}

export const VerdictGlyph = ({ v, size = 12 }: { v: string | null | undefined; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" style={{ flex: 'none' }}>
    <Marker v={v} cx={6} cy={6} r={4.5} />
  </svg>
)

const clampX = (v: number, w: number, pad: number) => pad + ((Math.min(SCALE_MAX, Math.max(SCALE_MIN, v)) - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)) * (w - 2 * pad)

/* ---------------------------------------------------------------------------------------------- */

export function RowWaterfall({ d, width = 104, height = 18 }: { d: WaterfallInput; width?: number; height?: number }) {
  // Two lanes: what was given (nominal bar, top) and what it measures to (effective marker, bottom).
  // The baseline rule crosses both, so "which side of the line" reads without a number.
  const pad = 3
  const id = useId()
  const x = (v: number) => clampX(v, width, pad)
  const barY = 2
  const barH = 6
  const laneY = 13.5
  const bx = x(BASELINE)
  const st = steps(d)
  const posted = isPosted(d.comp_source, d.posted_min, d.posted_max)
  const title = st
    ? `${posted ? 'posted' : '~est'} ${k(d.nominal!)} → tax ${signedK(st[0].delta)} → purchasing power ${signedK(st[1].delta)} → premium ${signedK(st[2].delta)} = ${k(d.effective!)} vs ${k(BASELINE)} baseline`
    : d.scored === false
      ? 'not scored yet — next cycle'
      : d.out_of_scope
        ? `not measured — out of scope: ${d.out_of_scope}`
        : 'no comp signal: nothing posted, nothing on file, no prior'
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title} style={{ display: 'block', overflow: 'visible' }}>
      <title>{title}</title>
      <defs>
        <pattern id={`h${id}`} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="4" height="4" fill="var(--surface-2)" />
          <rect width="1.2" height="4" fill="var(--ink-25)" />
        </pattern>
      </defs>
      {/* decision zone: floor → instant-yes */}
      <rect x={x(FLOOR)} y={0} width={x(INSTANT_YES) - x(FLOOR)} height={height} fill="var(--ink-06)" />
      {/* result lane track */}
      <line x1={pad} x2={width - pad} y1={laneY} y2={laneY} stroke="var(--ink-12)" strokeWidth="1" strokeDasharray={st ? undefined : '1.5 2.5'} />
      {st ? (
        <>
          {/* given: nominal bar — solid (posted) or hatched (inferred) */}
          <rect x={pad} y={barY} width={Math.max(0, x(d.nominal!) - pad)} height={barH} fill={posted ? 'var(--ink-25)' : `url(#h${id})`} />
          {posted && d.posted_min && d.posted_max && d.posted_min !== d.posted_max ? (
            <rect x={x(d.posted_min)} y={barY} width={Math.max(1, x(d.posted_max) - x(d.posted_min))} height={barH} fill="var(--ink-50)" />
          ) : null}
          {/* measured: drift from nominal to effective, then the verdict marker */}
          <line x1={x(d.nominal!)} x2={x(d.nominal!)} y1={barY + barH} y2={laneY} stroke="var(--ink-25)" strokeWidth="1" />
          <line x1={x(d.nominal!)} x2={x(d.effective!)} y1={laneY} y2={laneY} stroke="var(--ink-50)" strokeWidth="1.5" />
          <Marker v={d.verdict} cx={x(d.effective!)} cy={laneY} r={4.5} />
          {d.nominal! > SCALE_MAX && <text x={width - 2} y={barY + barH} fontSize="9" fontFamily="var(--font-mono)" fill="var(--ink-70)">›</text>}
          {d.effective! > SCALE_MAX && <text x={width + 1} y={laneY + 3.5} fontSize="9" fontFamily="var(--font-mono)" fill="var(--ink-70)">›</text>}
          {d.effective! < SCALE_MIN && <text x={-6} y={laneY + 3.5} fontSize="9" fontFamily="var(--font-mono)" fill="var(--ink-70)">‹</text>}
        </>
      ) : (
        <text x={pad} y={barY + barH + 1} fontSize="9" fontFamily="var(--font-mono)" fill="var(--ink-50)">
          {d.scored === false ? 'pending' : d.out_of_scope ? 'n/a' : 'no comp'}
        </text>
      )}
      {/* THE baseline: drawn last, full ink, crosses both lanes */}
      <line x1={bx} x2={bx} y1={0} y2={height} stroke="var(--ink)" strokeWidth="1.5" strokeDasharray="2.5 1.5" />
    </svg>
  )
}

/* ---------------------------------------------------------------------------------------------- */

export function DetailWaterfall({ d, realTerms, metroName, taxLabel }: { d: WaterfallInput; realTerms?: number | null; metroName?: string; taxLabel?: string }) {
  const id = useId()
  const width = 640
  const pad = 28
  const rowH = 30
  const labelW = 136
  const top = 18 // room for the baseline label above the first row
  const x = (v: number) => labelW + clampX(v, width - labelW, pad)
  const st = steps(d)
  const posted = isPosted(d.comp_source, d.posted_min, d.posted_max)
  const rows: { label: string; sub?: string; from: number; to: number; kind: 'bar' | 'step' | 'total' }[] = st
    ? [
        { label: posted ? 'posted base' : 'estimated base', sub: posted ? 'employer range' : `${d.comp_source?.replace(/_/g, ' ')} · ${Math.round((d.confidence ?? 0) * 100)}% conf`, from: SCALE_MIN, to: d.nominal!, kind: 'bar' },
        { label: 'tax vs baseline', sub: taxLabel, from: st[0].from, to: st[0].to, kind: 'step' },
        { label: 'purchasing power', sub: metroName ? `${metroName} vs the baseline metro, on after-tax money` : 'on after-tax money', from: st[1].from, to: st[1].to, kind: 'step' },
        { label: 'location premium', sub: 'your stated value', from: st[2].from, to: st[2].to, kind: 'step' },
        { label: 'effective value', sub: 'drives the ranking', from: SCALE_MIN, to: d.effective!, kind: 'total' },
      ]
    : []
  const height = top + (rows.length || 1) * rowH + 36
  const bx = x(BASELINE)
  if (!st) {
    return (
      <div className="state small">
        <div className="font-medium">{d.scored === false ? 'Not scored yet' : d.out_of_scope ? 'Not measured — out of scope' : 'No comp signal for this posting'}</div>
        <div className="quiet mt-1">
          {d.scored === false
            ? 'It arrived in the last fetch and is queued for the next scoring pass (every 15 minutes). The verdict and waterfall appear then.'
            : d.out_of_scope
              ? `${d.out_of_scope}. Comp is only estimated for roles you could actually apply to, so the verdict is withheld rather than guessed. If the scope call is wrong, the rule that made it is in config/title_rules.yaml (edit → radar rescore --replay).`
              : 'Nothing posted, no recent range at this company, no DOL prior for this title and metro, and too few peers to model. The comp sub-score runs at neutral and the verdict is withheld — it cannot be “better” or “worse” than the baseline until one of those exists. Logging a number you learn (offer, recruiter call) fixes it: radar note <id> "base 120000".'}
        </div>
      </div>
    )
  }
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Comp waterfall against the baseline" style={{ display: 'block', maxWidth: width, minWidth: 540, overflow: 'visible' }}>
      <defs>
        <pattern id={`H${id}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="6" height="6" fill="var(--surface-2)" />
          <rect width="1.5" height="6" fill="var(--ink-25)" />
        </pattern>
      </defs>
      {/* baseline label above the rule */}
      <text x={bx} y={10} textAnchor="middle" fontSize="10.5" fontFamily="var(--font-mono)" fontWeight="500" fill="var(--ink)">
        {k(BASELINE)} · baseline
      </text>
      <g transform={`translate(0 ${top})`}>
      {/* decision zone band + axis ticks */}
      <rect x={x(FLOOR)} y={0} width={x(INSTANT_YES) - x(FLOOR)} height={rows.length * rowH} fill="var(--ink-06)" />
      {axisTicks().map((v) => (
        <g key={v}>
          <line x1={x(v)} x2={x(v)} y1={rows.length * rowH} y2={rows.length * rowH + 4} stroke="var(--ink-25)" />
          <text x={x(v)} y={rows.length * rowH + 16} textAnchor="middle" fontSize="10" fontFamily="var(--font-mono)" fill="var(--ink-50)">
            {k(v)}
          </text>
        </g>
      ))}
      <text x={x(FLOOR)} y={rows.length * rowH + 30} textAnchor="middle" fontSize="10" fontFamily="var(--font-mono)" fill="var(--ink-50)">floor</text>
      <text x={x(INSTANT_YES)} y={rows.length * rowH + 30} textAnchor="middle" fontSize="10" fontFamily="var(--font-mono)" fill="var(--ink-50)">instant-yes</text>
      {/* baseline rule, full height */}
      <line x1={bx} x2={bx} y1={-4} y2={rows.length * rowH + 6} stroke="var(--ink)" strokeWidth="1.5" strokeDasharray="4 2.5" />
      {rows.map((r, i) => {
        const y = i * rowH + rowH / 2
        const x0 = x(Math.min(r.from, r.to))
        const x1 = x(Math.max(r.from, r.to))
        const delta = r.to - r.from
        const neg = r.kind === 'step' && delta < 0
        return (
          <g key={r.label}>
            <text x={labelW - 10} y={y - (r.sub ? 2 : -4)} textAnchor="end" fontSize="12" fontFamily="var(--font-body)" fontWeight="500" fill="var(--ink)">
              {r.label}
            </text>
            {r.sub && (
              <text x={labelW - 10} y={y + 11} textAnchor="end" fontSize="10" fontFamily="var(--font-body)" fill="var(--ink-50)">
                {r.sub}
              </text>
            )}
            {r.kind === 'bar' && (
              <>
                <rect x={x0} y={y - 7} width={Math.max(0, x1 - x0)} height={14} fill={posted ? 'var(--ink-25)' : `url(#H${id})`} />
                {posted && d.posted_min && d.posted_max && d.posted_min !== d.posted_max ? <rect x={x(d.posted_min)} y={y - 7} width={Math.max(1, x(d.posted_max) - x(d.posted_min))} height={14} fill="var(--ink-50)" /> : null}
                <text x={x1 + 6} y={y + 4} fontSize="11.5" fontFamily="var(--font-mono)" fontWeight="500" fill="var(--ink)">
                  {posted ? '' : '~'}{k(r.to)}
                  {posted && d.posted_min && d.posted_max && d.posted_min !== d.posted_max ? ` (${k(d.posted_min)}–${k(d.posted_max)})` : ''}
                </text>
              </>
            )}
            {r.kind === 'step' && (
              <>
                {/* connector from previous level */}
                <line x1={x(r.from)} x2={x(r.from)} y1={y - rowH / 2} y2={y} stroke="var(--ink-25)" strokeWidth="1" />
                {Math.abs(delta) > 0 ? (
                  <rect x={x0} y={y - 6} width={Math.max(1, x1 - x0)} height={12} fill={neg ? 'var(--alert)' : 'var(--better)'} opacity="0.85" />
                ) : (
                  <line x1={x0 - 3} x2={x0 + 3} y1={y} y2={y} stroke="var(--ink-25)" strokeWidth="1.5" />
                )}
                <text x={neg && x0 - 44 > labelW ? x0 - 6 : x1 + 6} y={y + 4} textAnchor={neg && x0 - 44 > labelW ? 'end' : 'start'} fontSize="11.5" fontFamily="var(--font-mono)" fontWeight="500" fill={neg ? 'var(--alert)' : 'var(--better)'}>
                  {delta === 0 ? '±$0' : signedK(delta)}
                </text>
              </>
            )}
            {r.kind === 'total' && (
              <>
                <line x1={x(r.to)} x2={x(r.to)} y1={y - rowH / 2} y2={y - 8} stroke="var(--ink-25)" strokeWidth="1" />
                <rect x={x0} y={y - 8} width={Math.max(0, x1 - x0)} height={16} fill={verdictColor(d.verdict)} opacity="0.22" />
                <line x1={x1} x2={x1} y1={y - 8} y2={y + 8} stroke={verdictColor(d.verdict)} strokeWidth="2.5" />
                <Marker v={d.verdict} cx={x1 + 11} cy={y} r={6} />
                <text x={x1 + 22} y={y + 4} fontSize="12.5" fontFamily="var(--font-mono)" fontWeight="500" fill="var(--ink)">
                  {k(r.to)} <tspan fill="var(--ink-50)">({signedK(r.to - BASELINE)} vs {k(BASELINE)})</tspan>
                </text>
              </>
            )}
          </g>
        )
      })}
      {realTerms != null && (
        <g>
          <line x1={x(BASELINE + realTerms)} x2={x(BASELINE + realTerms)} y1={rows.length * rowH - 4} y2={rows.length * rowH + 4} stroke="var(--ink-50)" strokeWidth="1" strokeDasharray="1.5 1.5" />
        </g>
      )}
      </g>
    </svg>
  )
}

export const fromPosting = (p: { in_scope?: number | null; scope_reason?: string | null; base_est?: number | null; base_col_adjusted?: number | null; location_utility_premium?: number | null; tax_delta_vs_baseline?: number | null; effective_value?: number | null; base_posted_min?: number | null; base_posted_max?: number | null; comp_source?: string | null; comp_confidence?: number | null; beats_baseline?: string | null; floor_result?: string | null; scored_at?: string | null }): WaterfallInput => ({
  nominal: p.base_est,
  col_adjusted: p.base_col_adjusted,
  premium: p.location_utility_premium,
  tax_delta: p.tax_delta_vs_baseline,
  effective: p.effective_value,
  posted_min: p.base_posted_min,
  posted_max: p.base_posted_max,
  comp_source: p.comp_source,
  confidence: p.comp_confidence,
  verdict: p.beats_baseline,
  scored: p.floor_result != null || p.scored_at != null || p.in_scope === 0,
  out_of_scope: p.in_scope === 0 ? (p.scope_reason ?? 'out of scope') : null,
})
