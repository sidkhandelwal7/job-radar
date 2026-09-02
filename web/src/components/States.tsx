/** Empty and failure states. An empty result is an invitation to widen; an error says what happened
 *  and what to do. Nothing here apologizes, nothing is vague. */
import type { ListResponse } from '../lib/api'

export function EmptyState({ q, view, data, chips, onRemoveChip, onMaster }: { q: string; view: string; data: ListResponse; chips: string[]; onRemoveChip: (c: string) => void; onMaster: () => void }) {
  const hidden = view === 'default' ? data.suppressed : 0
  return (
    <div className="state max-w-xl">
      <div className="font-medium">
        0 of {data.master_total.toLocaleString()} match{q ? <> <code className="mono">{q}</code></> : ' this view'}.
      </div>
      <div className="quiet small mt-1">
        {hidden > 0 && (
          <div>
            {hidden.toLocaleString()} rows are suppressed by the filtered view (duplicates, out of scope, floor failures, dismissed).{' '}
            <button className="underline" style={{ color: 'var(--better)' }} onClick={onMaster}>
              Show the master view
            </button>
            .
          </div>
        )}
        {chips.length > 1 && (
          <div className="mt-1">
            Widen by dropping a term:{' '}
            {chips.map((c) => (
              <button key={c} className="chip mono mr-1" onClick={() => onRemoveChip(c)} title={`remove ${c}`}>
                − {c}
              </button>
            ))}
          </div>
        )}
        {chips.length <= 1 && hidden === 0 && <div>Nothing in the master list matches. Check the spelling of the field (see “All filters…”) or run <code className="mono">radar fetch</code> if the sources look stale in Health.</div>}
      </div>
    </div>
  )
}

export function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  const offline = /fetch|network|failed to|ECONN|load failed/i.test(message)
  return (
    <div className="state state-alert max-w-xl small" role="alert">
      <div className="font-medium" style={{ color: 'var(--alert)' }}>
        ■ {offline ? 'The local server on :8787 did not answer.' : 'The request failed.'}
      </div>
      <div className="quiet mt-1">
        {offline ? (
          <>
            <code className="mono">radar serve</code> is not running, or it is restarting. Start it, or check <code className="mono">radar launchd-status</code>.
          </>
        ) : (
          <code className="mono">{message}</code>
        )}
      </div>
      {retry && (
        <button className="btn btn-sm mt-2" onClick={retry}>
          Try again
        </button>
      )}
    </div>
  )
}

export function DeadLink({ since, siblings, onOpenSibling }: { since: string | null; siblings: { id: number; company_name: string; title: string; url_status: string }[]; onOpenSibling: (id: number) => void }) {
  const live = siblings.filter((s) => s.url_status !== 'dead')
  return (
    <div className="state state-alert small" role="status">
      <div className="font-medium" style={{ color: 'var(--alert)' }}>
        ■ This link is dead{since ? ` since ${since.slice(0, 10)}` : ''}.
      </div>
      <div className="quiet mt-1">
        The employer's board no longer serves this req, which almost always means it closed.{' '}
        {live.length ? (
          <>
            The same role is still live from another source:{' '}
            {live.map((s) => (
              <button key={s.id} className="underline mr-2" style={{ color: 'var(--better)' }} onClick={() => onOpenSibling(s.id)}>
                open it
              </button>
            ))}
          </>
        ) : (
          <>If you already applied, the application record is unaffected. Otherwise dismiss it with the reason “closed”.</>
        )}
      </div>
    </div>
  )
}
