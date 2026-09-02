export const money = (v: number | null | undefined, k = true): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (k && Math.abs(v) >= 1000) return `$${(v / 1000).toFixed(0)}k`
  return `$${Math.round(v).toLocaleString()}`
}
export const signedMoney = (v: number | null | undefined): string => {
  if (v === null || v === undefined) return '—'
  const s = `$${Math.abs(Math.round(v)).toLocaleString()}`
  return v < 0 ? `−${s}` : `+${s}`
}
export const pct = (v: number | null | undefined, d = 0): string => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)
export const num = (v: number | null | undefined, d = 2): string => (v === null || v === undefined ? '—' : v.toFixed(d))
export const ago = (iso: string | null | undefined): string => {
  if (!iso) return 'never'
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}
export const day = (iso: string | null | undefined): string => (iso ? iso.slice(0, 10) : '—')
export const METRO_LABEL: Record<string, string> = {
  new_york: 'New York', san_francisco: 'SF Bay Area', seattle: 'Seattle', washington_dc: 'DC metro', boston: 'Boston', austin: 'Austin', chicago: 'Chicago',
  los_angeles: 'Los Angeles', denver: 'Denver', pittsburgh: 'Pittsburgh', remote: 'Remote', us_unknown: 'US (metro ?)', multiple: 'Multiple',
}
export const metro = (m: string | null | undefined): string => (m ? METRO_LABEL[m] ?? m.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : '?')
export const CATEGORY_LABEL: Record<string, string> = {
  big_tech_swe: 'Big Tech', bank_and_exchange_tech: 'Banks & exchanges', fintech_infrastructure: 'Fintech infra', elite_infra_startup: 'Infra startup',
  defense_and_gov_tech: 'Defense / gov', quant_dev_research_trading: 'Quant', ai_lab: 'AI lab', other: 'Other',
}
export const category = (c: string | null | undefined): string => (c ? CATEGORY_LABEL[c] ?? c : '—')
export const VERDICT_LABEL: Record<string, string> = { clearly_better: 'clearly better', arguably_better: 'arguably better', worse: 'worse' }
export const ACTION_LABEL: Record<string, string> = {
  apply_today: 'Apply today', apply_this_week: 'Apply this week', watch: 'Watch', get_referral_first: 'Get a referral first', blocked_needs_prep: 'Blocked, needs prep', verify_link: 'Link dead — verify before dismissing', needs_review: 'Needs review (unenriched)',
}
export const linkStatus = (p: { url_status?: string; url_verify_method?: string | null; url_last_verified_at?: string | null }): { text: string; color: string; dead: boolean } => {
  const when = ago(p.url_last_verified_at)
  if (p.url_status === 'live' && p.url_verify_method === 'source_presence') return { text: `listed at source ${when}`, color: 'var(--better)', dead: false }
  if (p.url_status === 'live' || p.url_status === 'redirected') return { text: `verified live ${when}`, color: 'var(--better)', dead: false }
  if (p.url_status === 'dead') return { text: `dead ${when} — req likely closed`, color: 'var(--alert)', dead: true }
  return { text: `unverified ${when}`, color: 'var(--ink-50)', dead: false }
}
