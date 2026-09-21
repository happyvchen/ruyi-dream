import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, '..', '')
  const apiBaseUrl = rootEnv.VITE_API_BASE_URL || rootEnv.PUBLIC_BACKEND_URL || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    define: {
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(apiBaseUrl),
      'import.meta.env.VITE_UPLOAD_CONCURRENCY': JSON.stringify(process.env.VITE_UPLOAD_CONCURRENCY || rootEnv.UPLOAD_CONCURRENCY || '2'),
    },
    server: {
      port: 5173,
    },
  }
})
