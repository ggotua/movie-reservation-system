/// <reference types="vite/client" />

// @fontsource-variable/big-shoulders-display ships its CSS entry point without
// type declarations (unlike the IBM Plex package), so name it here.
declare module '@fontsource-variable/big-shoulders-display'

/**
 * Environment variables this app reads. Every VITE_* value is compiled into
 * the public bundle, so none of them may hold a secret.
 * They are read only by src/lib/config.ts.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_CURRENCY?: string
  readonly VITE_CINEMA_TIMEZONE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
