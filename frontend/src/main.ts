import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { captureTokenFromUrl } from './api/auth'
import { initI18n } from './i18n'
import { router } from './router'

// Vue Flow's own styles first, so `base.css` can restyle its internals afterwards.
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'
import './styles/index.css'

// Must run before the first request goes out, so the token is available to the API
// client and the WebSocket handshake alike.
captureTokenFromUrl()

// The interface language is restored before the first render, so a session in another
// language never paints a frame of English first.
void initI18n().then(() => {
  createApp(App).use(createPinia()).use(router).mount('#app')
})
