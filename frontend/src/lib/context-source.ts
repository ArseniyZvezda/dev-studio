import type { ContextInventory, ContextSource } from '@/api/types'
import { hasMessage, t } from '@/i18n'

/** Outer record indices are zero-based physical JSONL line indices (§10.4). */
export function contextFieldLocator(origin: string, journalPath?: string) {
  const row = journalPath && /^(?:journal|response_item|compacted|session_meta)\[(\d+)\]\./.exec(origin)
  return row ? t('contextViewer.journalLine', { line: Number(row[1]) + 1, field: origin }) : origin
}

/** The inventory is evidence of where a fragment was read. Only provided_by identifies a built-in source (§10.4). */
export function contextSourceLabel(source: ContextSource, inventory: ContextInventory['origin'], compact = false) {
  if (source.category === 'unattributed') return ''

  const kindKey = `contextViewer.sourceKinds.${source.source_kind}`
  const kind = hasMessage(kindKey) ? t(kindKey) : undefined
  const detail = source.source_detail || source.label
  const record = [kind, detail].filter(Boolean).join(' · ')
  const origin = t(`contextViewer.origins.${inventory}`)
  const builtin = source.provided_by
    ? t('contextViewer.builtinSource', { backend: t(`nav.agentBackends.${source.provided_by}`) })
    : undefined
  return compact ? record || builtin || origin : [origin, record, builtin].filter(Boolean).join(' · ')
}
