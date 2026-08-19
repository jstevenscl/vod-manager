// Shared redesign primitives (section card / KPI tile / status pill / chip /
// quota bar / input styling) -- used across the admin VodManager DVR
// screens and the end-user portal (src/pages/Portal.tsx) so both share one
// visual language instead of the portal reinventing its own badge/number
// treatment. Pulled out of pages/VodManager.tsx (where these originated)
// specifically so the portal doesn't have to import that whole 6800+ line
// admin file just to get a status pill.

import { Card, CardContent } from '@/components/ui/card'

export function SectionCard({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="space-y-3">
        <h2 className="text-sm font-bold flex items-center gap-2">
          <span className="flex items-center justify-center w-6 h-6 rounded-md bg-primary/10 text-primary shrink-0 [&_svg]:w-3.5 [&_svg]:h-3.5">{icon}</span>
          {title}
        </h2>
        {children}
      </CardContent>
    </Card>
  )
}

export function KpiTile({ icon, label, value, note, noteTone = 'default' }: {
  icon: React.ReactNode
  label: string
  value: React.ReactNode
  note?: React.ReactNode
  noteTone?: 'default' | 'warn'
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-3.5 shadow-sm hover:border-primary/30 transition-colors">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <span className="flex items-center justify-center w-5 h-5 rounded bg-primary/10 text-primary shrink-0 [&_svg]:w-3 [&_svg]:h-3">{icon}</span>
        {label}
      </div>
      <div className="mt-2 text-2xl font-bold tracking-tight tabular-nums">{value}</div>
      {note && <div className={`mt-0.5 text-xs ${noteTone === 'warn' ? 'text-warning' : 'text-muted-foreground/70'}`}>{note}</div>}
    </div>
  )
}

export function StatusPill({ label, tone = 'success', icon }: {
  label: React.ReactNode
  tone?: 'success' | 'warning' | 'destructive' | 'info'
  icon?: React.ReactNode
}) {
  const toneCls = {
    success: 'text-success bg-success/10 border-success/25',
    warning: 'text-warning bg-warning/10 border-warning/25',
    destructive: 'text-destructive bg-destructive/10 border-destructive/25',
    info: 'text-primary bg-primary/10 border-primary/25',
  }[tone]
  const dotCls = { success: 'bg-success', warning: 'bg-warning', destructive: 'bg-destructive', info: 'bg-primary' }[tone]
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-semibold ${toneCls}`}>
      {icon ?? <span className={`w-1.5 h-1.5 rounded-full ${dotCls}`} />}
      {label}
    </span>
  )
}

export function Chip({ children, tone = 'default' }: { children: React.ReactNode; tone?: 'default' | 'rec' }) {
  return (
    <span className={
      tone === 'rec'
        ? 'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-bold uppercase tracking-wide text-destructive bg-destructive/10'
        : 'inline-flex items-center rounded border border-border bg-secondary px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground'
    }>
      {children}
    </span>
  )
}

// Two-tone actual/virtual usage bar -- solid fill for real local bytes,
// hatched fill for backfill/pointer bytes that cost no extra disk. Used by
// the admin Users section (per-DVR-provider quotas) and the portal's own
// Usage view (a single person's own quota), both feeding it the same
// {actual,virtual,quota} GB shape computed from vod_db.dvr_user_disk_usage_bytes.
export function QuotaBar({ actualGB, virtualGB, quotaGB }: {
  actualGB: number | null
  virtualGB: number | null
  quotaGB: number | null
}) {
  const usageGB = (actualGB ?? 0) + (virtualGB ?? 0)
  const quotaDenomGB = quotaGB ?? Math.max(1, usageGB || 1)
  return (
    <div>
      <div className="flex items-center justify-between text-[11px] font-medium text-muted-foreground mb-1">
        <span>Disk quota</span>
        <span className="tabular-nums">{usageGB.toFixed(1)}{quotaGB != null ? ` / ${quotaGB.toFixed(0)} GB` : ' GB (no quota set)'}</span>
      </div>
      <div className="h-1.5 rounded-full bg-secondary overflow-hidden flex">
        {actualGB != null && actualGB > 0 && (
          <div className="h-full bg-primary" style={{ width: `${Math.min(100, (actualGB / quotaDenomGB) * 100)}%` }} />
        )}
        {virtualGB != null && virtualGB > 0 && (
          <div
            className="h-full bg-primary/35"
            style={{
              width: `${Math.min(100 - Math.min(100, ((actualGB ?? 0) / quotaDenomGB) * 100), (virtualGB / quotaDenomGB) * 100)}%`,
              backgroundImage: 'repeating-linear-gradient(135deg, hsl(var(--primary)/0.5) 0 3px, transparent 3px 6px)',
            }}
          />
        )}
      </div>
      {virtualGB != null && virtualGB > 0.01 && (
        <div className="text-[10.5px] text-muted-foreground mt-1">{virtualGB.toFixed(1)}GB virtual/backfill (hatched)</div>
      )}
    </div>
  )
}

export function inputCls(extra = '') {
  return `h-8 px-2.5 rounded border border-border bg-background text-sm outline-none focus:ring-1 focus:ring-primary ${extra}`
}
