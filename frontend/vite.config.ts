import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 开发期前端跑在 5173，后端在 8000。经代理转发 /api 使浏览器同源，
// HttpOnly 会话 Cookie 因此在开发期与生产期行为一致（生产由 Caddy 转发）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
