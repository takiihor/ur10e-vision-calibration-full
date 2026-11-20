import { createApp } from 'vue';
import PrimeVue from 'primevue/config';
import App from './App.vue';
import Nord from './nord.js';
import 'primeicons/primeicons.css';
import './operator.css';

createApp(App).use(PrimeVue, {
  theme: { preset: Nord, options: { darkModeSelector: false } },
  csp: { nonce: document.querySelector('meta[name="style-nonce"]').content },
}).mount('#app');
