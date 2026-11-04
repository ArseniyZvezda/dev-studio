import type { UpdateStatus } from '@/api/types'

export interface DesktopUpdateStatus extends UpdateStatus {
  downloaded: boolean
  progress: number
}

export interface DesktopWindowState {
  maximized: boolean
  focused: boolean
}

declare global {
  interface Window {
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
