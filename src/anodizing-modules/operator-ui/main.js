import { createApp } from 'vue';
import PrimeVue from 'primevue/config';
import Aura from '@primeuix/themes/aura';
import App from './App.vue';
import 'primeicons/primeicons.css';
import './operator.css';

createApp(App).use(PrimeVue, {
  theme: { preset: Aura, options: { darkModeSelector: false } },
  csp: { nonce: document.querySelector('meta[name="style-nonce"]').content },
}).mount('#app');
