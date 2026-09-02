import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { api, type FacetValue, type ListResponse, type Posting } from '../lib/api'
import { CATEGORY_LABEL, METRO_LABEL, VERDICT_LABEL, category, metro, money, num } from '../lib/format'
import { parseHash, setHash } from '../lib/url'
import { ActionTag, ApplyLink, Base, LinkBadge, Verdict, Where } from '../components/PostingBits'
import { RowWaterfall, fromPosting } from '../components/CompWaterfall'
import { EmptyState, ErrorState } from '../components/States'

const PAGE = 300
const PRIMARY_FACETS: { key: string; label: string; field: string; labels?: Record<string, string>; pin?: string[] }[] = [
  { key: 'beats_baseline', label: 'Better than baseline?', field: 'beats_baseline', labels: VERDICT_LABEL },
  { key: 'base_bucket', label: 'Base salary', field: '__base' },
  { key: 'metro', label: 'Location', field: 'metro', labels: METRO_LABEL, pin: ['new_york', 'san_francisco', 'seattle'] },
  { key: 'category', label: 'Target category', field: 'category', labels: CATEGORY_LABEL },
  { key: 'company', label: 'Company', field: 'company' },
  { key: 'dream', label: 'Dream list', field: '__dream' },
  { key: 'days_to_close', label: 'Days until close', field: '__close' },
  { key: 'work_mode', label: 'Work mode', field: 'work_mode' },
  { key: 'status', label: 'Status', field: 'status' },
  { key: 'has_posted_comp', label: 'Comp range', field: '__posted' },
  { key: 'fit_bucket', label: 'Resume fit', field: '__fit' },
  { key: 'queue_action', label: 'Queue action', field: 'queue_action' },
]
const SORTS: [string, string][] = [
  ['priority', 'priority'], ['rank', 'queue rank'], ['composite', 'score'], ['effective', 'effective value'], ['base', 'base'], ['fit', 'fit'], ['urgency', 'urgency'], ['ev', 'EV'],
  ['first_seen', 'first seen'], ['posted', 'posted'], ['days_to_close', 'deadline'], ['company', 'company'], ['winnability', 'winnability'], ['career_capital', 'career capital'],
]

function termFor(facet: string, value: string): string {
  switch (facet) {
    case '__base':
      return { '150k+': 'base >= 150k', '120-150k': 'base >= 120k base < 150k', '100-120k': 'base >= 100k base < 120k', '85-100k': 'base >= 85k base < 100k', '<85k': 'base < 85k' }[value] ?? ''
    case '__dream':
      return value === 'dream' ? 'dream' : 'NOT dream'
    case '__close':
      return { '≤7d': 'days_to_close <= 7', '≤30d': 'days_to_close <= 30', '30d+': 'days_to_close > 30' }[value] ?? ''
    case '__posted':
      return value === 'posted' ? 'has_posted_comp' : 'NOT has_posted_comp'
    case '__fit':
      return { strong: 'fit >= 0.7', ok: 'fit >= 0.5 fit < 0.7', weak: 'fit < 0.5' }[value] ?? ''
    case 'company':
      return `company:"${value}"`
    default:
      return `${facet}:${value}`
  }
}

export default function TableView({ onOpen }: { onOpen: (id: number) => void }) {
  const route = parseHash()
  const q = route.params.get('q') ?? ''
  const view = route.params.get('view') ?? 'default'
  const sort = route.params.get('sort') ?? 'priority'
  const [draft, setDraft] = useState(q)
  const [data, setData] = useState<ListResponse | null>(null)
  const [rows, setRows] = useState<Posting[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [showWhy, setShowWhy] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [fields, setFields] = useState<Awaited<ReturnType<typeof api.fields>> | null>(null)
  const [presets, setPresets] = useState<Awaited<ReturnType<typeof api.presets>>>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [cursor, setCursor] = useState(0)
  const parentRef = useRef<HTMLDivElement>(null)

  useEffect(() => setDraft(q), [q])
  useEffect(() => {
    api.fields().then(setFields).catch(() => undefined)
    api.presets().then(setPresets).catch(() => undefined)
  }, [])

  const load = useCallback(() => {
    setLoading(true)
    setErr(null)
    api
      .postings(q, view, sort, PAGE, 0, true)
      .then((d) => {
        setData(d)
        setRows(d.rows)
        setSelected(new Set())
        setCursor(0)
      })
      .catch((e) => setErr(String(e.message ?? e)))
      .finally(() => setLoading(false))
  }, [q, view, sort])
  useEffect(load, [load])

  // windowed loading: fetch the next page when the virtualizer nears the end
  const virt = useVirtualizer({ count: rows.length, getScrollElement: () => parentRef.current, estimateSize: () => 40, overscan: 30 })
  const items = virt.getVirtualItems()
  useEffect(() => {
    const last = items[items.length - 1]
    if (!data || loading || !last) return
    if (last.index >= rows.length - 40 && rows.length < data.total) {
      setLoading(true)
      api
        .postings(q, view, sort, PAGE, rows.length, false)
        .then((d) => setRows((r) => [...r, ...d.rows]))
        .finally(() => setLoading(false))
    }
  }, [items, rows.length, data, loading, q, view, sort])

  const setQ = (nq: string) => setHash('table', { q: nq })
  const addTerm = (t: string) => t && setQ(`${q} ${t}`.trim())
  const chips = useMemo(() => {
    // tokenize outside quotes, then glue `field op value` and `NOT x` into one chip each
    const toks = q.match(/"[^"]*"|\S+/g) ?? []
    const out: string[] = []
    for (let i = 0; i < toks.length; i++) {
      let t = toks[i]
      if (/^(NOT|-)$/i.test(t) && toks[i + 1]) t = `${t} ${toks[++i]}`
      while (/^(>=|<=|!=|=|>|<|~)$/.test(toks[i + 1] ?? '')) t = `${t} ${toks[++i]} ${toks[++i] ?? ''}`.trim()
      out.push(t)
    }
    return out
  }, [q])
  const removeChip = (chip: string) => setQ(chips.filter((c) => c !== chip).join(' '))

  const bulk = async (action: Record<string, unknown>) => {
    if (!selected.size) return
    await api.bulk([...selected], action)
    load()
  }

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        if (e.key === 'Escape') (e.target as HTMLElement).blur()
        return
      }
      const p = rows[cursor]
      if (e.key === '/') {
        document.getElementById('q')?.focus()
      } else if (e.key === 'j') setCursor((c) => Math.min(rows.length - 1, c + 1))
      else if (e.key === 'k') setCursor((c) => Math.max(0, c - 1))
      else if (e.key === 'Enter' && p) onOpen(p.id)
      else if (e.key === 'o' && p) window.open(p.apply_url, '_blank')
      else if (e.key === 'a' && p) api.action(p.id, { action: 'applied' }).then(load)
      else if (e.key === 's' && p) api.action(p.id, { action: p.status === 'shortlisted' ? 'unshortlist' : 'shortlist' }).then(load)
      else if (e.key === 'x' && p) {
        const reason = window.prompt('Dismiss — why?', '')
        if (reason !== null) api.action(p.id, { action: 'dismiss', reason }).then(load)
      } else if (e.key === ' ' && p) {
        setSelected((s) => {
          const n = new Set(s)
          if (n.has(p.id)) n.delete(p.id)
          else n.add(p.id)
          return n
        })
      } else return
      e.preventDefault()
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  })
  useEffect(() => {
    if (rows[cursor]) virt.scrollToIndex(cursor, { align: 'auto' })
  }, [cursor, rows, virt])

  return (
    <div className="flex h-[calc(100vh-48px)] flex-col">
      {/* search + master/filtered toggle */}
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2" style={{ borderColor: 'var(--ink-12)', background: 'var(--surface)' }}>
        <form
          className="flex w-full grow items-center gap-2 sm:w-auto"
          onSubmit={(e) => {
            e.preventDefault()
            setQ(draft)
          }}
        >
          <input id="q" className="mono w-full min-w-40 !text-[12.5px]" placeholder='base > 110k category:big_tech metro:nyc -requires_clearance   ( / to focus )' value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="Query" />
          <button className="btn" type="submit">
            Search
          </button>
        </form>
        <select className="min-w-0 grow sm:grow-0" value="" onChange={(e) => { const p = presets.find((x) => String(x.id) === e.target.value); if (p) setHash('table', { q: p.query, sort: p.sort?.split(' ')[0]?.replace('_score', '') ?? 'priority', view: ['Everything (Master)', 'Floor Failures Audit', 'Applied', 'Watchlist', 'High-Pay International', 'Stretch (2 YoE)'].includes(p.name) ? 'master' : 'default' }) }} aria-label="Presets">
          <option value="">Presets…</option>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select className="min-w-0 grow sm:grow-0" value={sort} onChange={(e) => setHash('table', { sort: e.target.value })} aria-label="Sort">
          {SORTS.map(([k, v]) => (
            <option key={k} value={k}>
              sort: {v}
            </option>
          ))}
        </select>
        <button className="btn hidden sm:inline-flex" onClick={() => { const name = window.prompt('Save this filter as:'); if (name) api.saveFilter({ name, query: q, sort }).then(() => api.presets().then(setPresets)) }}>
          Save filter
        </button>
        <a className="btn no-underline hidden sm:inline-flex" href={`/api/export.csv?${new URLSearchParams({ q, view })}`}>
          CSV
        </a>
      </div>

      {/* counts line: the "214 of 8,391 — 8,177 suppressed [why]" contract */}
      {data && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-1.5 small" style={{ borderColor: 'var(--ink-12)' }}>
          <span className="whitespace-nowrap">
            <span className="num text-[13px]">{data.total.toLocaleString()}</span> <span className="muted">of</span> <span className="num text-[13px]">{data.master_total.toLocaleString()}</span>
            {view === 'default' ? (
              <>
                <span className="muted"> — {data.suppressed.toLocaleString()} suppressed </span>
                <button className="underline underline-offset-2" style={{ color: 'var(--better)' }} onClick={() => setShowWhy((s) => !s)}>
                  why
                </button>
              </>
            ) : (
              <span className="muted"> · master view — nothing hidden</span>
            )}
          </span>
          <span className="inline-flex overflow-hidden rounded border caption" style={{ borderColor: 'var(--ink-25)' }} role="group" aria-label="View">
            <button className={`px-2 py-0.5 ${view === 'default' ? 'chip-on' : ''}`} onClick={() => setHash('table', { view: 'default' })}>
              Filtered
            </button>
            <button className={`px-2 py-0.5 ${view === 'master' ? 'chip-on' : ''}`} onClick={() => setHash('table', { view: 'master' })}>
              Master
            </button>
          </span>
          {chips.map((c) => (
            <span key={c} className="chip mono">
              {c}
              <button className="ml-1 muted" onClick={() => removeChip(c)} title="remove" aria-label={`remove ${c}`}>
                ×
              </button>
            </span>
          ))}
          {selected.size > 0 && (
            <span className="ml-auto flex items-center gap-1">
              <span className="muted">{selected.size} selected:</span>
              <button className="btn" onClick={() => bulk({ action: 'shortlist' })}>shortlist</button>
              <button className="btn" onClick={() => bulk({ action: 'applied' })}>mark applied</button>
              <button className="btn" onClick={() => { const reason = window.prompt('Dismiss all selected — why?'); if (reason !== null) bulk({ action: 'dismiss', reason }) }}>dismiss…</button>
              <button className="btn" onClick={() => bulk({ action: 'snooze', days: 7 })}>snooze 7d</button>
              <button className="btn" onClick={() => { const t = window.prompt('Tag (comma-separated):'); if (t) bulk({ action: 'tag', tags: t.split(',').map((x) => x.trim()) }) }}>tag…</button>
            </span>
          )}
          {showWhy && view === 'default' && (
            <div className="w-full card small">
              <div className="mb-1 font-medium">Hidden from the filtered view (click a reason to see those rows):</div>
              <ul className="flex flex-wrap gap-2">
                {data.suppressions.map((s) => (
                  <li key={s.key}>
                    <button className="chip" onClick={() => setHash('table', { view: 'master', q: `${q} ${{ duplicate: 'NOT cluster_canonical', delisted: 'delisted', out_of_scope: 'NOT in_scope', floor: 'floor:fail', dismissed: 'status:dismissed', applied: 'status:applied', snoozed: 'snoozed' }[s.key] ?? ''}`.trim() })}>
                      {s.label}: <b>{s.count.toLocaleString()}</b>
                    </button>
                  </li>
                ))}
              </ul>
              <div className="mt-1 muted">Filters are views, never deletions. Every row is still in the master list.</div>
            </div>
          )}
        </div>
      )}
      {err && (
        <div className="p-3">
          <ErrorState message={err} retry={load} />
        </div>
      )}

      <div className="flex min-h-0 grow">
        {/* primary facet panel */}
        <aside className="hidden w-60 shrink-0 overflow-y-auto border-r px-3 py-2 small md:block" style={{ borderColor: 'var(--ink-12)' }}>
          {data?.facets &&
            PRIMARY_FACETS.map((f) => {
              const vals = (data.facets?.[f.key] ?? []) as FacetValue[]
              if (!vals.length) return null
              const pinned = f.pin ? [...vals.filter((v) => f.pin!.includes(v.value)), ...vals.filter((v) => !f.pin!.includes(v.value))] : vals
              return (
                <div key={f.key} className="mb-3">
                  <div className="mb-1 caption font-medium uppercase tracking-wider muted">{f.label}</div>
                  {pinned.slice(0, 10).map((v) => (
                    <button key={v.value} className="flex w-full items-center justify-between gap-2 rounded px-1 py-0.5 text-left hover:bg-[var(--ink-06)]" onClick={() => addTerm(termFor(f.field, v.value))}>
                      <span className="truncate">{f.labels?.[v.value] ?? (f.key === 'category' ? category(v.value) : f.key === 'metro' ? metro(v.value) : v.value)}</span>
                      <span className="num muted">{v.count.toLocaleString()}</span>
                    </button>
                  ))}
                </div>
              )
            })}
          <button className="btn w-full" onClick={() => setShowAll((s) => !s)}>
            {showAll ? 'Hide all filters' : 'All filters…'}
          </button>
          {showAll && fields && (
            <div className="mt-2 space-y-2">
              <div className="muted caption">Every field, grouped. Click to insert into the query, then edit the value.</div>
              {Object.entries(fields.groups).map(([g, fs]) => (
                <div key={g}>
                  <div className="caption font-medium uppercase tracking-wider muted">{g}</div>
                  <div className="flex flex-wrap gap-1">
                    {fs.map((f) => (
                      <button key={f.name} className="chip mono" title={f.help || f.kind} onClick={() => { setDraft((d) => `${d} ${f.name}${f.kind === 'bool' ? '' : f.kind === 'number' ? ' >= ' : ':'}`.trimStart()); document.getElementById('q')?.focus() }}>
                        {f.name}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </aside>

        {/* virtualized dense table */}
        <div ref={parentRef} className="min-w-0 grow overflow-auto">
          {data && data.total === 0 && !loading ? (
            <div className="p-4">
              <EmptyState q={q} view={view} data={data} chips={chips} onRemoveChip={removeChip} onMaster={() => setHash('table', { view: 'master' })} />
            </div>
          ) : (
          <table className="dense w-full md:min-w-[1180px]">
            <thead className="sticky top-0 z-10" style={{ background: 'var(--paper)' }}>
              <tr>
                <th className="w-6 !px-2 hidden sm:table-cell"></th>
                <th className="w-9 num hidden sm:table-cell">#</th>
                <th>Company · title</th>
                <th className="hidden lg:table-cell">Where</th>
                <th className="w-[116px] !px-1.5 sm:w-[124px] sm:!px-2">
                  <span className="inline-flex items-center gap-1">
                    vs baseline <span className="num normal-case tracking-normal" style={{ color: 'var(--ink-25)' }}>fixed scale</span>
                  </span>
                </th>
                <th className="num hidden sm:table-cell">Base</th>
                <th className="num hidden md:table-cell">Effective</th>
                <th className="!px-1.5 sm:!px-2"><span className="hidden md:inline">Verdict</span></th>
                <th className="num hidden lg:table-cell">Score</th>
                <th className="num hidden lg:table-cell">Fit</th>
                <th className="num hidden xl:table-cell">Urg</th>
                <th className="hidden md:table-cell">Action</th>
                <th className="hidden md:table-cell">Link</th>
              </tr>
            </thead>
            <tbody>
              {items.length > 0 && items[0].start > 0 && (
                <tr style={{ height: items[0].start }} aria-hidden>
                  <td colSpan={13} className="!p-0 !border-0 !h-auto" />
                </tr>
              )}
              {items.map((vi) => {
                const p = rows[vi.index]
                const active = vi.index === cursor
                return (
                  <tr key={p.id} data-index={vi.index} ref={virt.measureElement} className={`${active ? 'row-focus' : ''} ${p.status === 'dismissed' ? 'opacity-50' : ''}`} onClick={() => setCursor(vi.index)} aria-selected={active}>
                    <td className="!px-2 hidden sm:table-cell">
                      <input type="checkbox" checked={selected.has(p.id)} onChange={() => setSelected((s) => { const n = new Set(s); if (n.has(p.id)) n.delete(p.id); else n.add(p.id); return n })} aria-label={`select ${p.company_name} ${p.title}`} />
                    </td>
                    <td className="num muted hidden sm:table-cell">{p.apply_priority_rank ?? ''}</td>
                    <td className="max-w-[calc(100vw-170px)] sm:max-w-[260px] md:max-w-md">
                      <div className="flex items-baseline gap-1.5 truncate">
                        <span className="font-medium truncate" title={p.company_name}>
                          {p.is_dream_list ? <span title="dream list" style={{ color: 'var(--better)' }}>★ </span> : null}
                          {p.company_name}
                        </span>
                        {p.same_market_as_baseline_offer ? <span className="caption" title="same market as the baseline offer" style={{ color: 'var(--arguable)' }}>⚑</span> : null}
                        {p.cluster_size > 1 && <span className="caption muted" title="also listed via other sources">×{p.cluster_size}</span>}
                      </div>
                      <div className="truncate small quiet -mt-0.5">
                        <button className="text-left hover:underline truncate max-w-full" onClick={() => onOpen(p.id)}>
                          {p.status === 'shortlisted' && <span title="shortlisted" style={{ color: 'var(--arguable)' }}>★ </span>}
                          {p.title}
                        </button>
                      </div>
                    </td>
                    <td className="max-w-44 truncate hidden lg:table-cell small quiet">
                      <Where p={p} />
                    </td>
                    <td className="!px-1.5 sm:!px-2">
                      <RowWaterfall d={fromPosting(p)} />
                    </td>
                    <td className="num hidden sm:table-cell whitespace-nowrap">
                      <Base p={p} />
                    </td>
                    <td className="num hidden md:table-cell">{money(p.effective_value)}</td>
                    <td className="!px-1.5 sm:!px-2">
                      <span className="md:hidden"><Verdict v={p.beats_baseline} reason={p.beats_baseline_reason} short /></span>
                      <span className="hidden md:inline"><Verdict v={p.beats_baseline} reason={p.beats_baseline_reason} /></span>
                    </td>
                    <td className="num hidden lg:table-cell">{num(p.composite_score)}</td>
                    <td className="num hidden lg:table-cell">{num(p.fit_score)}</td>
                    <td className="num hidden xl:table-cell">{num(p.urgency_score)}</td>
                    <td className="hidden md:table-cell">
                      <ActionTag a={p.queue_action} />
                      {!p.in_scope && p.scope_reason && <span className="chip" title={p.scope_reason}>{p.scope_reason.slice(0, 28)}</span>}
                    </td>
                    <td className="whitespace-nowrap hidden md:table-cell">
                      <ApplyLink p={p} /> <LinkBadge p={p} />
                    </td>
                  </tr>
                )
              })}
              {items.length > 0 && virt.getTotalSize() - items[items.length - 1].end > 0 && (
                <tr style={{ height: virt.getTotalSize() - items[items.length - 1].end }} aria-hidden>
                  <td colSpan={13} className="!p-0 !border-0 !h-auto" />
                </tr>
              )}
            </tbody>
          </table>
          )}
          {loading && <div className="p-2 text-center muted small">loading…</div>}
          {data && rows.length >= data.total && data.total > 0 && <div className="p-2 text-center muted caption">end of {data.total.toLocaleString()} rows</div>}
        </div>
      </div>
    </div>
  )
}
