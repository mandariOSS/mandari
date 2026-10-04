// Typdeklarationen für Alpine-Plugins ohne eigene Typen (ambient, daher ohne Imports)

declare module '@alpinejs/collapse' {
  const collapse: import('alpinejs').PluginCallback
  export default collapse
}

declare module '@alpinejs/focus' {
  const focus: import('alpinejs').PluginCallback
  export default focus
}

// Lucide-Icons, die im Projekt vorkommen (Vite-Plugin frontend/vite/lucide-icons.ts)
declare module 'virtual:lucide-icons' {
  export const icons: import('lucide').Icons
}
