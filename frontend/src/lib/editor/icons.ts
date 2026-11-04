/** Callout icons, keyed like their names (`editor.callouts.*`) — §3.1's four kinds, and no others. */

import { AlertTriangle, Info, Lightbulb, OctagonAlert } from 'lucide-vue-next'

export const CALLOUT_ICONS: Record<string, typeof Info> = {
  note: Info,
  tip: Lightbulb,
  warning: AlertTriangle,
  danger: OctagonAlert,
}
