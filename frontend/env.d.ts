/// <reference types="vite/client" />

/** Exact build tag (normalized), or short commit hash when untagged (§2.5). */
declare const __STUDIO_VERSION__: string

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

interface ImportMetaEnv {
  /** Backend origin the dev server proxies to; unused in production builds. */
  readonly VITE_BACKEND?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
