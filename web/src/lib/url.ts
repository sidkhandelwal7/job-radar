// Hash-based URL state: #/table?q=...&view=master&sort=priority — bookmarkable, shareable.
export type Route = { view: string; params: URLSearchParams }
export const parseHash = (): Route => {
  const h = window.location.hash.replace(/^#\/?/, '')
  const [path, qs] = h.split('?')
  return { view: path || 'queue', params: new URLSearchParams(qs || '') }
}
export const setHash = (view: string, params?: Record<string, string | null | undefined>): void => {
  const cur = parseHash()
  const p = new URLSearchParams(cur.view === view ? cur.params : '')
  for (const [k, v] of Object.entries(params || {})) {
    if (v === null || v === undefined || v === '') p.delete(k)
    else p.set(k, v)
  }
  const qs = p.toString()
  window.location.hash = `/${view}${qs ? `?${qs}` : ''}`
}
