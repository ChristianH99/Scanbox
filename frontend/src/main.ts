import './app.css';
import { mount } from 'svelte';
import App from './App.svelte';
import { initTheme } from './lib/stores/session.svelte';
import { i18n } from './lib/i18n/index.svelte';

initTheme();
document.documentElement.lang = i18n.locale;

const app = mount(App, { target: document.getElementById('app')! });

export default app;
