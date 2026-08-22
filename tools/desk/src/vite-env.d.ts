/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DESK_TOKEN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
