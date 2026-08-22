import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  base: '/desk/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: 'dist',
  },
  server: {
    proxy: {
      '/internal': {
        // macOS AirPlay often owns :5000 — override with DESK_API=http://127.0.0.1:5055
        target: process.env.DESK_API || 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
    },
  },
})
