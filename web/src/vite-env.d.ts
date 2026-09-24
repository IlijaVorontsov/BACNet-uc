/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" builds or serves the app with the in-browser mock backend. */
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
