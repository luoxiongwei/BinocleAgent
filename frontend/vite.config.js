import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8010',
      '/tasks': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/inputs': 'http://127.0.0.1:8000',
      '/qa': 'http://127.0.0.1:8000',
      '/chat': 'http://127.0.0.1:8000',
      '/feedback': 'http://127.0.0.1:8000',
      '/memory': 'http://127.0.0.1:8000',
    },
  },
})
