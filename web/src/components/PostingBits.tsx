import type { Posting } from '../lib/api'
import { ACTION_LABEL, VERDICT_LABEL, linkStatus, metro, money } from '../lib/format'
import { VerdictGlyph, isPosted } from './CompWaterfall'

/** Verdict = shape + hue + weight. The shape is what a colorblind reader keys on. */
export const Verdict = ({ v, reason, short = false }: { v: string | null; reason?: string | null; short?: boolean }) => {
  if (!v) return <span className="verdict verdict-none" title="no verdict yet"><VerdictGlyph v={null} /> {short ? '' : 'unscored'}</span>
  const cls = v === 'clearly_better' ? 'verdict-clearly' : v === 'arguably_better' ? 'verdict-arguably' : 'verdict-worse'
  return (
    <span className={`verdict ${cls}`} title={reason ?? ''}>
      <VerdictGlyph v={v} />
      {short ? null : <span>{VERDICT_LABEL[v] ?? v}</span>}
    </span>
  )
}

export const ActionTag = ({ a }: { a: string | null }) => {
  if (!a) return null
  const cls = a === 'apply_today' ? 'tag-today' : a === 'apply_this_week' ? 'tag-week' : a === 'get_referral_first' ? 'tag-referral' : a === 'blocked_needs_prep' ? 'tag-blocked' : a === 'verify_link' ? 'tag-dead' : a === 'needs_review' ? 'tag-referral' : 'tag-watch'
  return <span className={`tag ${cls}`}>{ACTION_LABEL[a] ?? a}</span>
}

/** Base salary. A posted range is ink and plain; an inferred estimate is "~", lighter, hatched
 *  underneath, with its confidence printed. They must never look the same (§3.9, §17.9). */
export const Base = ({ p }: { p: Posting }) => {
  const posted = isPosted(p.comp_source, p.base_posted_min, p.base_posted_max)
  if (posted && (p.base_posted_min || p.base_posted_max)) {
    const lo = p.base_posted_min, hi = p.base_posted_max
    return (
      <span className="num" title={`Posted by the employer (${p.comp_source})`}>
        {lo && hi && lo !== hi ? `${money(lo)}–${money(hi)}` : money(hi ?? lo)}
      </span>
    )
  }
  if (p.base_est)
    return (
      <span className="est num" title={`Estimated from ${p.comp_source?.replace(/_/g, ' ')} at ${Math.round((p.comp_confidence ?? 0) * 100)}% confidence. Not a posted number.`}>
        ~{money(p.base_est)}
        <span className="caption muted"> {Math.round((p.comp_confidence ?? 0) * 100)}%</span>
      </span>
    )
  return <span className="caption muted">no comp</span>
}

export const Where = ({ p }: { p: Posting }) => {
  const names: string[] = []
  for (const l of p.locations ?? []) {
    const n = l.kind === 'remote' ? `Remote${l.state ? ` (${l.state})` : ''}` : l.metro_name ?? l.raw
    if (n && !names.includes(n)) names.push(n)
  }
  const text = names.length ? names.slice(0, 2).join('; ') + (names.length > 2 ? ` +${names.length - 2}` : '') : metro(p.primary_metro)
  return (
    <span title={names.join('; ')}>
      {text}
      {p.work_mode === 'hybrid' ? <span className="muted"> · hybrid</span> : p.work_mode === 'remote' ? <span className="muted"> · remote</span> : null}
    </span>
  )
}

export const LinkBadge = ({ p }: { p: Posting }) => {
  const s = linkStatus(p)
  return <span className="caption" style={{ color: s.color }}>{s.text}</span>
}

export const copy = async (text: string) => {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    window.prompt('Copy link:', text)
  }
}

export const ApplyLink = ({ p, big = false }: { p: Posting; big?: boolean }) => (
  <span className="inline-flex items-center gap-1">
    <a href={p.apply_url} target="_blank" rel="noreferrer" className={big ? 'btn btn-primary no-underline' : 'no-underline'} title={p.apply_url}>
      {big ? 'Open posting ↗' : 'apply ↗'}
    </a>
    <button className="btn btn-sm !px-1.5" title="Copy link (for referral requests)" onClick={() => copy(p.apply_url)}>
      ⧉
    </button>
  </span>
)

export const ScoreBar = ({ label, value, weight, why }: { label: string; value: number | null; weight?: number; why?: string }) => (
  <div className="flex items-center gap-2 small" title={why}>
    <span className="w-32 shrink-0 capitalize quiet">{label.replace(/_/g, ' ')}</span>
    <div className="h-1.5 flex-1 rounded-sm" style={{ background: 'var(--ink-06)' }}>
      <div className="h-1.5 rounded-sm" style={{ width: `${Math.round((value ?? 0) * 100)}%`, background: 'var(--ink-50)' }} />
    </div>
    <span className="num w-9 text-right">{value === null || value === undefined ? '—' : value.toFixed(2)}</span>
    {weight !== undefined && <span className="num w-10 muted caption">×{weight.toFixed(2)}</span>}
  </div>
)
