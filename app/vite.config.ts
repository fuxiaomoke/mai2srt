import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import path from 'node:path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, 'src'),
    },
  },
  server: {
    // dedicated port: 5173 is the Vite DEFAULT and trans-jimaku-web (and any
    // other local Vite project) sits on it too. A bumped port here would be
    // SILENT (vite just moves to 5174) while tauri.conf.json's devUrl still
    // loads whatever holds 5173 -- another project's UI or a leaked old
    // vite of ours. strictPort makes any collision fail loudly instead.
    port: 5183,
    host: 'localhost',
    strictPort: true,
  },
})
