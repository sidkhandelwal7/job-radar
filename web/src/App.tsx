import { useEffect, useState } from 'react'
import { parseHash, setHash } from './lib/url'
import Queue from './views/Queue'
import TableView from './views/TableView'
import Detail from './views/Detail'
import Applications from './views/Applications'
import Calendar from './views/Calendar'
import ConfigView from './views/ConfigView'
import Health from './views/Health'
import { api } from './lib/api'
import { setBaseline } from './components/CompWaterfall'

const NAV: [string, string][] = [
  ['queue', 'Queue'],
  ['table', 'Table'],
  ['applications', 'Applications'],
  ['calendar', 'Calendar'],
  ['config', 'Config'],
  ['health', 'Health'],
]

export default function App() {
  const [route, setRoute] = useState(parseHash())
  const [baselineLabel, setBaselineLabel] = useState('')
  useEffect(() => {
    // the baseline salary and comp gates drive every waterfall; load them once
    api
      .config()
      .then((c) => {
        const base = Number(c.baseline.base_salary)
        setBaseline(base, Number(c.comp_gates.hard_floor), Number(c.comp_gates.instant_yes))
        setBaselineLabel(`vs $${Math.round(base / 1000)}k · ${String(c.baseline.metro).replace(/_/g, ' ')}`)
      })
      .catch(() => setBaselineLabel(''))
  }, [])
  const [dark, setDark] = useState<boolean>(() => localStorage.getItem('theme') === 'dark' || (!localStorage.getItem('theme') && window.matchMedia('(prefers-color-scheme: dark)').matches))
  useEffect(() => {
    const h = () => setRoute(parseHash())
    window.addEventListener('hashchange', h)
    return () => window.removeEventListener('hashchange', h)
  }, [])
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (e.key === '/' && route.view !== 'table') {
        setHash('table')
        setTimeout(() => document.getElementById('q')?.focus(), 50)
        e.preventDefault()
      }
      if (e.key === 'g') {
        // g then a letter: q/t/a/c
        const once = (e2: KeyboardEvent) => {
          const m: Record<string, string> = { q: 'queue', t: 'table', a: 'applications', c: 'calendar', h: 'health' }
          if (m[e2.key]) setHash(m[e2.key])
          window.removeEventListener('keydown', once)
        }
        window.addEventListener('keydown', once)
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [route.view])

  const open = (id: number) => setHash('posting', { id: String(id) })
  const back = () => window.history.length > 1 ? window.history.back() : setHash('queue')
  const view = route.view
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 flex h-12 items-center gap-1 border-b px-2 sm:gap-2 sm:px-3" style={{ borderColor: 'var(--ink-12)', background: 'var(--surface)' }}>
        <a href="#/queue" className="display mr-1 whitespace-nowrap no-underline text-[15px] sm:mr-2" style={{ color: 'var(--ink)' }}>
          Job Radar <span className="num caption muted ml-1 hidden md:inline">{baselineLabel}</span>
        </a>
        <nav className="flex min-w-0 gap-0.5 overflow-x-auto small" aria-label="Views" style={{ scrollbarWidth: 'none' }}>
          {NAV.map(([k, label]) => {
            const on = view === k || (k === 'table' && view === 'posting')
            return (
              <a key={k} href={`#/${k}`} className="whitespace-nowrap rounded px-1.5 py-1 no-underline sm:px-2" style={{ color: on ? 'var(--ink)' : 'var(--ink-70)', background: on ? 'var(--ink-06)' : 'transparent', fontWeight: on ? 600 : 400, boxShadow: on ? 'inset 0 -2px 0 var(--ink)' : 'none' }} aria-current={on ? 'page' : undefined}>
                {label}
              </a>
            )
          })}
        </nav>
        <span className="grow" />
        <span className="hidden caption muted md:inline">
          <span className="kbd">/</span> search · <span className="kbd">g</span>+<span className="kbd">q</span>/<span className="kbd">t</span>/<span className="kbd">a</span> go
        </span>
        <button className="btn btn-sm" onClick={() => setDark((d) => !d)} title="Toggle dark mode" aria-label="Toggle dark mode">
          {dark ? 'Light' : 'Dark'}
        </button>
      </header>
      {view === 'queue' && <Queue onOpen={open} />}
      {view === 'table' && <TableView onOpen={open} />}
      {view === 'posting' && <Detail id={Number(route.params.get('id'))} onBack={back} />}
      {view === 'applications' && <Applications onOpenPosting={open} />}
      {view === 'calendar' && <Calendar onOpenPosting={open} />}
      {view === 'config' && <ConfigView />}
      {view === 'health' && <Health />}
    </div>
  )
}
