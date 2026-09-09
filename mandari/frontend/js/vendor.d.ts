// Typdeklarationen für Alpine-Plugins ohne eigene Typen (ambient, daher ohne Imports)

declare module '@alpinejs/collapse' {
  const collapse: import('alpinejs').PluginCallback
  export default collapse
}

declare module '@alpinejs/focus' {
  const focus: import('alpinejs').PluginCallback
  export default focus
}
