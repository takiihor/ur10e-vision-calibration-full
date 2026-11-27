import { createApp } from 'vue';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';
import App from './App.vue';
import Alucard from './alucard.js';
import { showUiIds } from './ui-ids-overlay.js';
import 'primeicons/primeicons.css';
import './layout/sakai.css';
import './operator.css';

createApp(App).use(PrimeVue, {
  theme: { preset: Alucard, options: { darkModeSelector: false } },
  pt: { select: { option: ({ instance, context }) => ({
    'data-ui': 'select.option',
    'data-ui-key': instance.getOptionValue(context.option),
  }) } },
  csp: { nonce: document.querySelector('meta[name="style-nonce"]').content },
}).use(ConfirmationService).use(ToastService).mount('#app');

showUiIds();
