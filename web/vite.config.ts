import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The web app calls `/api/v1/...` on its own origin. In production nginx maps `/api/v1` to the
// FastAPI app's `/v1`; in dev Vite does the same rewrite against 127.0.0.1:8300.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8300',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
  preview: { port: 4173 },
  build: {
    target: 'es2022',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          ethers: ['ethers'],
          react: ['react', 'react-dom'],
        },
      },
    },
  },
});
