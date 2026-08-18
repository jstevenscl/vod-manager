import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'

// In-app replacement for window.confirm(), shared across every page
// (VodManager, Portal, Settings). A native confirm() blocks the whole tab
// at the browser level (not the page's own DOM) -- unstyled, fully
// page-freezing for real users, and impossible for any browser-driving
// tool to click through (they can only interact with the page's own
// render tree, and a native dialog sits outside it). Module-scoped store
// so any component anywhere can call askConfirm directly without prop
// drilling; each top-level page mounts one <ConfirmDialogHost /> to
// actually render it.
type ConfirmState = { message: string; onConfirm: () => void } | null
let _setConfirmDialog: ((state: ConfirmState) => void) | null = null

export function askConfirm(message: string, onConfirm: () => void) {
  _setConfirmDialog?.({ message, onConfirm })
}

export function ConfirmDialogHost() {
  const [confirmDialog, setConfirmDialog] = useState<ConfirmState>(null)
  useEffect(() => {
    _setConfirmDialog = setConfirmDialog
    return () => { _setConfirmDialog = null }
  }, [])
  if (!confirmDialog) return null
  return createPortal(
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/80 p-4" onClick={() => setConfirmDialog(null)}>
      <div
        className="relative bg-card border border-border rounded-xl overflow-hidden w-full max-w-sm shadow-2xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          className="absolute top-2 right-2 text-muted-foreground hover:text-foreground transition-colors p-1 rounded hover:bg-accent z-10"
          onClick={() => setConfirmDialog(null)}
        >
          <X size={16} />
        </button>
        <div className="p-5 space-y-4">
          <p className="text-sm whitespace-pre-wrap pr-4">{confirmDialog.message}</p>
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="outline" onClick={() => setConfirmDialog(null)}>Cancel</Button>
            <Button size="sm" onClick={() => { confirmDialog.onConfirm(); setConfirmDialog(null) }}>Confirm</Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
