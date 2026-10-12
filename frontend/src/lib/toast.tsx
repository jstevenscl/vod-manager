import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react'

// Short, non-blocking confirmation that fades on its own -- for "it worked"
// feedback that doesn't need an OK click (use notify() from '@/lib/confirm'
// for messages the user must read). Module-scoped store like askConfirm, so
// any component can call toast() directly; one <ToastHost /> is mounted at
// the app root (main.tsx) and renders for every page.
type ToastTone = 'success' | 'error' | 'info'
interface ToastItem { id: number; tone: ToastTone; message: string }

const DURATION_MS = 3000
let _nextId = 1
let _push: ((item: ToastItem) => void) | null = null

export function toast(message: string, tone: ToastTone = 'success') {
  _push?.({ id: _nextId++, tone, message })
}
toast.success = (message: string) => toast(message, 'success')
toast.error = (message: string) => toast(message, 'error')
toast.info = (message: string) => toast(message, 'info')

const TONE = {
  success: { cls: 'text-success border-success/40', icon: <CheckCircle2 size={14} className="shrink-0" /> },
  error: { cls: 'text-destructive border-destructive/40', icon: <CircleAlert size={14} className="shrink-0" /> },
  info: { cls: 'text-primary border-primary/40', icon: <Info size={14} className="shrink-0" /> },
}

export function ToastHost() {
  const [items, setItems] = useState<ToastItem[]>([])
  const dismiss = (id: number) => setItems((cur) => cur.filter((t) => t.id !== id))

  useEffect(() => {
    _push = (item) => {
      setItems((cur) => [...cur.slice(-3), item])
      setTimeout(() => dismiss(item.id), DURATION_MS)
    }
    return () => { _push = null }
  }, [])

  if (!items.length) return null
  return createPortal(
    <div className="fixed bottom-4 right-4 z-[300] flex flex-col items-end gap-2 pointer-events-none">
      {items.map((t) => (
        <div
          key={t.id}
          role="status"
          className={`pointer-events-auto flex items-center gap-2 max-w-sm rounded-md border bg-card px-3 py-2 text-sm font-medium shadow-lg ${TONE[t.tone].cls}`}
        >
          {TONE[t.tone].icon}
          <span className="flex-1">{t.message}</span>
          <button className="text-muted-foreground hover:text-foreground" onClick={() => dismiss(t.id)} aria-label="Dismiss">
            <X size={12} />
          </button>
        </div>
      ))}
    </div>,
    document.body,
  )
}
