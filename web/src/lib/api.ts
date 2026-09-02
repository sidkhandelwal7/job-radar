/* eslint-disable @typescript-eslint/no-explicit-any */
export type Posting = {
  id: number; company_name: string; title: string; primary_metro: string | null; work_mode: string | null; locations: { raw: string; metro_name?: string; kind?: string; state?: string }[]
  base_posted_min: number | null; base_posted_max: number | null; base_est: number | null; base_est_low: number | null; base_est_high: number | null; comp_source: string | null; comp_confidence: number | null
  effective_value: number | null; real_terms_vs_baseline: number | null; base_col_adjusted: number | null; location_utility_premium: number | null; tax_delta_vs_baseline: number | null; tc_year1_est: number | null; beats_baseline: string | null; beats_baseline_reason: string | null
  composite_score: number | null; comp_score: number | null; career_capital_score: number | null; fit_score: number | null; winnability_score: number | null; location_score: number | null; culture_score: number | null
  urgency_score: number | null; priority: number | null; apply_priority_rank: number | null; queue_action: string | null; ev_estimate: number | null; p_offer: number | null
  prep_archetype: string | null; prep_hours_est: number | null; target_category: string | null; company_tier: number | null; is_dream_list: number
  role_family: string | null; role_subfamily: string | null; seniority: string | null; is_new_grad: number; is_stretch: number; program_type: string | null; employment_type: string | null
  in_scope: number | null; scope_reason: string | null; floor_result: string | null; floor_fail_reasons: string[]; hard_blockers: string[]
  requires_clearance: number | null; requires_advanced_degree: number | null; min_years_experience: number | null; sponsorship: string | null; graduation_window: string | null
  referral_likelihood: string | null; referral_secured: number; same_market_as_baseline_offer: number; status: string; dismiss_reason: string | null; snooze_until: string | null; starred: number; tags_user: string[]
  apply_url: string; canonical_url: string | null; url_status: string; url_last_verified_at: string | null; url_verify_method: string | null; url_age: string
  posted_at: string | null; first_seen_at: string; first_seen_age: string; last_seen_at: string; delisted_at: string | null; application_deadline: string | null; repost_of_id: number | null
  source: string; source_provider: string; description_fetched: number; tech_tags: string[]; matched_strengths: { strength: string; evidence?: string | null }[]; gaps: { gap: string; severity: string; note?: string }[]
  cluster_id: number | null; cluster_size: number; is_cluster_canonical: number; is_international_only: number
}
export type PostingDetail = Posting & {
  description_md: string | null; score_explanation: Record<string, any> | null; requirements: Record<string, any> | null; siblings: any[]; events: any[]; applications: any[]; link_checks: any[]
  requirement_checklist: { item: string; kind: string; status: string }[]; duplicate_warning: any; notes_md: string | null; company_slug: string | null; beats_baseline_decomposition: Record<string, any> | null
}
export type Suppression = { key: string; label: string; count: number }
export type FacetValue = { value: string; count: number }
export type ListResponse = { rows: Posting[]; total: number; master_total: number; suppressed: number; view: string; suppressions: Suppression[]; facets?: Record<string, FacetValue[]>; offset: number; limit: number }

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...init })
  if (!r.ok) {
    let msg = r.statusText
    try { msg = (await r.json()).detail ?? msg } catch { /* ignore */ }
    throw new Error(msg)
  }
  return r.json() as Promise<T>
}
export const api = {
  postings: (q: string, view: string, sort: string, limit = 200, offset = 0, facets = true) =>
    j<ListResponse>(`/api/postings?${new URLSearchParams({ q, view, sort, limit: String(limit), offset: String(offset), facets: String(facets) })}`),
  posting: (id: number) => j<PostingDetail>(`/api/postings/${id}`),
  kit: (id: number) => j<{ posting_id: number; kit_md: string | null; kit_at: string | null }>(`/api/postings/${id}/kit`),
  draftKit: (id: number, force = false) => j<{ posting_id: number; kit_md: string; kit_at: string; cached: boolean }>(`/api/postings/${id}/kit?force=${force}`, { method: 'POST' }),
  ops: () => j<any>('/api/ops'),
  action: (id: number, body: Record<string, unknown>) => j<{ ok: boolean; duplicate?: any; application_id?: number }>(`/api/postings/${id}/action`, { method: 'POST', body: JSON.stringify(body) }),
  bulk: (ids: number[], action: Record<string, unknown>) => j<{ results: any[] }>(`/api/postings/bulk`, { method: 'POST', body: JSON.stringify({ ids, action }) }),
  verify: (id: number) => j<Record<string, any>>(`/api/postings/${id}/verify`, { method: 'POST' }),
  queue: (action?: string) => j<{ rows: Posting[]; dead: Posting[]; counts: Record<string, number>; total: number; today_cap: number; applications_per_week: number }>(`/api/queue${action ? `?action=${action}` : ''}`),
  applications: () => j<{ rows: any[]; stats: any; follow_ups_due: any[]; ghosted_candidates: any[] }>(`/api/applications`),
  autofill: (url: string) => j<any>(`/api/applications/autofill`, { method: 'POST', body: JSON.stringify({ url }) }),
  createApplication: (body: Record<string, unknown>) => j<any>(`/api/applications`, { method: 'POST', body: JSON.stringify(body) }),
  patchApplication: (id: number, body: Record<string, unknown>) => j<any>(`/api/applications/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  calendar: () => j<any>(`/api/calendar`),
  config: () => j<any>(`/api/config`),
  previewConfig: (changes: Record<string, unknown>, commit = false) => j<any>(`/api/config/preview`, { method: 'POST', body: JSON.stringify({ changes, commit }) }),
  presets: () => j<{ id: number; name: string; query: string; sort: string | null; is_preset: number; alert_tier: string | null }[]>(`/api/presets`),
  saveFilter: (body: Record<string, unknown>) => j<any>(`/api/saved-filters`, { method: 'POST', body: JSON.stringify(body) }),
  fields: () => j<{ groups: Record<string, { name: string; kind: string; help: string; aliases: string }[]>; sortable: string[] }>(`/api/fields`),
  health: () => j<any>(`/api/health`),
}
