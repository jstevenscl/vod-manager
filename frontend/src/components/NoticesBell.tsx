import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, Check } from 'lucide-react'
import api from '@/lib/api'
import { toast } from '@/lib/toast'

// notices that stay until acknowledged (stalled / failed Radarr-Sonarr imports)
interface Notice {
  id: number; kind: string; severity: 'info' | 'warn' | 'error'; title: string; body: string | null
  job_id: number | null; created_at: string
}

const SEVERITY_CLS = { info: 'text-primary', warn: 'text-amber-500', error: 'text-destructive' }

export default function NoticesBell() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const seen = useRef<Set<number>>(new Set())
  const box = useRef<HTMLDivElement>(null)
  const query = useQuery<{ open: Notice[]; count: number }>({
    queryKey: ['vod-notices'],
    queryFn: () => api.get('/vod/notices/').then((r) => r.data),
    refetchInterval: 60000,
  })
  const ack = useMutation({
    mutationFn: (id: number) => api.post(`/vod/notices/${id}/ack/`),
    onSettled: () => qc.invalidateQueries({ queryKey: ['vod-notices'] }),
  })
  const items = query.data?.open ?? []

  // one toast per notice this browser hasn't shown yet; the bell keeps it until acknowledged
  useEffect(() => {
    for (const n of items) {
      if (seen.current.has(n.id)) continue
      seen.current.add(n.id)
      toast(n.title, n.severity === 'error' ? 'error' : 'info')
    }
  }, [items])

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  return (
    <div className="relative" ref={box}>
      <button
        className="relative text-muted-foreground hover:text-foreground transition-colors p-1.5 rounded hover:bg-accent"
        title={items.length ? `${items.length} notice${items.length === 1 ? '' : 's'} to acknowledge` : 'No notices'}
        onClick={() => setOpen((v) => !v)}
      >
        <Bell size={15} />
        {items.length > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-0.5 rounded-full bg-destructive text-[9px] font-bold leading-[15px] text-white text-center">
            {items.length > 99 ? '99+' : items.length}
          </span>
        )}
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-80 max-h-96 overflow-y-auto rounded-md border border-border bg-card shadow-lg z-50 text-xs">
          {items.length === 0 && <p className="p-3 text-muted-foreground">Nothing needs your attention.</p>}
          {items.map((n) => (
            <div key={n.id} className="border-b border-border/50 last:border-0 p-2.5 space-y-1">
              <div className={`font-medium break-words ${SEVERITY_CLS[n.severity] ?? ''}`}>{n.title}</div>
              {n.body && <div className="text-muted-foreground break-words">{n.body}</div>}
              <div className="flex items-center justify-between gap-2">
                <span className="text-[10px] text-muted-foreground">{new Date(Number(n.created_at) * 1000).toLocaleString()}</span>
                <button
                  className="flex items-center gap-1 px-1.5 py-0.5 rounded border border-border hover:bg-accent disabled:opacity-50"
                  disabled={ack.isPending}
                  onClick={() => ack.mutate(n.id)}
                >
                  <Check size={11} /> Acknowledge
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
