import type { RendererCloseRequest, RendererCloseAck } from '@/desktop/close-barrier'
import type { DirectoryListing, PathInspection, PickerStatus, PickResult, RecentProject, UpdateStatus } from '@/api/types'

/** Durable page only: no project handle, epoch, credentials or editor state (§5.3.6). */
export interface DesktopPageDescriptor {
  path: string
  query?: Partial<Record<'path' | 'task' | 'chat' | 'assign', string>>
}
export type DesktopPageRestoreStatus = 'restored' | 'invalid' | 'unavailable' | 'failed' | 'stale'
export interface DesktopPageIdentity {
  tabId: string
  tabRevision: number
  transferGeneration: number
}
export interface DesktopPageRestoreRequest extends DesktopPageIdentity {
  requestId: string
  page: DesktopPageDescriptor
}
export interface DesktopPageRestoreAck extends DesktopPageIdentity {
  requestId: string
  status: DesktopPageRestoreStatus
}

export interface DesktopUpdateStatus extends UpdateStatus {
  downloaded: boolean
  progress: number
}

export interface DesktopWindowState {
  maximized: boolean
  focused: boolean
}

/** Main-owned immutable binding, dev spec §5.3.2–5.3.4. No auth/native handles. */
export interface DesktopProjectBinding {
  readonly root: string
  readonly project_id: string
  readonly epoch: string
}

export interface DesktopTabSnapshot {
  readonly tabId: string
  readonly project: DesktopProjectBinding
  readonly ownerWindowId: string
  readonly state: 'loading' | 'ready' | 'crashed' | 'closing'
  readonly revision: number
  readonly transferGeneration: number
}

export interface DesktopTabsWindowSnapshot {
  readonly windowId: string
  readonly revision: number
  readonly tabIds: readonly string[]
  readonly activeTabId: string | null
  readonly insets: Readonly<{ top: number; right: number; bottom: number; left: number }>
}

export type DesktopTabsState = {
  readonly role: 'shell'
  readonly window: DesktopTabsWindowSnapshot
  readonly tabs: readonly DesktopTabSnapshot[]
  readonly error?: string | null
} | {
  readonly role: 'project'
  readonly windowId: string
  readonly windowRevision: number
  readonly tab: DesktopTabSnapshot
  readonly binding: DesktopProjectBinding
}

export interface DesktopTabsWindowRequest {
  windowId: string
  windowRevision: number
}

export interface DesktopTabsTabRequest extends DesktopTabsWindowRequest {
  tabId: string
  tabRevision: number
}

/** Client CSS pixels, not screen coordinates or physical pixels. Main validates
 * and converts these using the owning shell's bounds/zoom (dev spec §5.3.4). */
export interface DesktopTabDragRect {
  tabId: string
  x: number
  y: number
  width: number
  height: number
}

export interface DesktopTabsOpenRequest extends DesktopTabsWindowRequest {
  root: string
  create?: boolean
}

export type DesktopTabsPrepareCloseRequest =
  | (DesktopTabsWindowRequest & { target: 'window' })
  | (DesktopTabsTabRequest & { target: 'tab' })

export interface DesktopTabsBridge {
  reportPage?(request: DesktopPageIdentity & { page: DesktopPageDescriptor }): Promise<void>
  onRestorePage?(callback: (request: DesktopPageRestoreRequest) => void): () => void
  acknowledgePageRestore?(ack: DesktopPageRestoreAck): Promise<void>
  bootstrap(): Promise<DesktopTabsState>
  getState(): Promise<DesktopTabsState>
  /** Shell only. An already open root focuses its existing owner window. */
  openProject(request: DesktopTabsOpenRequest): Promise<DesktopTabSnapshot>
  /** Shell only; cannot activate another window's tab. */
  activateTab(request: DesktopTabsTabRequest): Promise<DesktopTabSnapshot>
  beginDrag(request: DesktopTabsTabRequest & { point: { x: number; y: number } }): Promise<{ token: string; expiresAt: number }>
  /** Cancellation only. The renderer never chooses a terminal target/outcome. */
  cancelDrag(request: DesktopTabsWindowRequest & { token: string }): Promise<void>
  /** Visible, viewport-clipped tab-button rectangles in registry order. Fully
   * offscreen tabs are omitted; tabId, not the array index, identifies order.
   * Gaps (including inline commands) are not tab/drop hit zones. The optional append interval spans the free
   * header space between the open button and the window controls. */
  setDragGeometry(request: DesktopTabsWindowRequest & { tabs: DesktopTabDragRect[]; appendStart?: number; appendEnd?: number }): Promise<void>
  detachTab(request: DesktopTabsTabRequest): Promise<DesktopTabSnapshot>
  reorderTab(request: DesktopTabsTabRequest & { index: number }): Promise<DesktopTabSnapshot>
  /** Barrier seam only; never closes a surface or quits. Defaults to false. */
  prepareClose(request: DesktopTabsPrepareCloseRequest): Promise<{ prepared: boolean }>
  /** Optional for compatibility with pre-close bridge adapters. Production preload supplies all three. */
  closeTab?(request: DesktopTabsTabRequest): Promise<{ closed: boolean }>
  /** Prepare/save, final fenced validation, then release on abort (same tuple). */
  onPrepareClose?(callback: (request: RendererCloseRequest) => void): () => void
  acknowledgeClose?(ack: RendererCloseAck): Promise<void>
  onStateChange(callback: (state: DesktopTabsState) => void): () => void
  recentProjects(): Promise<RecentProject[]>
  inspectPath(path: string): Promise<PathInspection>
  browseDirectory(path: string): Promise<DirectoryListing>
  pickerStatus(): Promise<PickerStatus>
  pickDirectory(initial?: string): Promise<PickResult>
  forgetRecent(root: string): Promise<void>
  /** Reserve the shell's menu hit zones without disposing any project view. */
  setMenuOpen(request: DesktopTabsWindowRequest & { open: boolean }): Promise<void>
}

declare global {
  interface Window {
    voxariumNotifications?: {
      getState(): Promise<{ supported: boolean; enabled: boolean }>
      setEnabled(enabled: boolean): Promise<{ supported: boolean; enabled: boolean }>
    }
    voxariumTabs?: DesktopTabsBridge
    voxariumStartup?: {
      getState(): Promise<{ supported: boolean; enabled: boolean }>
      setEnabled(enabled: boolean): Promise<{ supported: boolean; enabled: boolean }>
    }
    voxariumUpdates?: {
      getState(): Promise<DesktopUpdateStatus | null>
      check(): Promise<DesktopUpdateStatus>
      download(): Promise<DesktopUpdateStatus>
      install(): Promise<DesktopUpdateStatus>
      onStateChange(callback: (state: DesktopUpdateStatus) => void): () => void
    }
    voxariumWindow?: {
      getState(): Promise<DesktopWindowState>
      minimize(): Promise<DesktopWindowState>
      minimizeToTray?(): Promise<DesktopWindowState>
      setLocale?(locale: 'en' | 'ru'): Promise<DesktopWindowState>
      setProducerAttention?(waiting: boolean): Promise<DesktopWindowState>
      toggleMaximize(): Promise<DesktopWindowState>
      close(): Promise<DesktopWindowState | null>
      onStateChange(callback: (state: DesktopWindowState) => void): () => void
    }
  }
}
