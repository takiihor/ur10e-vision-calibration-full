import { createApp } from 'vue';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';
import App from './App.vue';
import Nord from './nord.js';
import 'primeicons/primeicons.css';
import './layout/sakai.css';
import './operator.css';

createApp(App).use(PrimeVue, {
  theme: { preset: Nord, options: { darkModeSelector: false } },
  csp: { nonce: document.querySelector('meta[name="style-nonce"]').content },
}).use(ConfirmationService).use(ToastService).mount('#app');
