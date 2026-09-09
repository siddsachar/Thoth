import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { bootstrapTheme, TOKENS } from './src/ui/theme-model';

const backend = new URL(
  process.env.ROW_BOT_DEV_BACKEND ?? 'http://127.0.0.1:8080',
);
if (
  backend.protocol !== 'http:' ||
  !['127.0.0.1', '[::1]', 'localhost'].includes(backend.hostname) ||
  backend.username ||
  backend.password ||
  backend.pathname !== '/' ||
  backend.search ||
  backend.hash
) {
  throw new Error('ROW_BOT_DEV_BACKEND must be a plain HTTP loopback origin');
}

export default defineConfig({
  base: '/app-v2/',
  plugins: [
    react(),
    {
      name: 'row-bot-theme-bootstrap',
      transformIndexHtml(html) {
        return html.replace(
          '<!-- row-bot-theme-bootstrap -->',
          `<script>(${bootstrapTheme.toString()})(${JSON.stringify(TOKENS)})</script>`,
        );
      },
    },
  ],
  server: {
    host: '127.0.0.1',
    strictPort: true,
    port: 5173,
    proxy: {
      '/api/v1': {
        target: backend.origin,
        changeOrigin: true,
        configure(proxy) {
          // Dev-only same-loopback proxy. Production access policy is untouched.
          proxy.on('proxyReq', (request) => {
            request.setHeader('Origin', backend.origin);
          });
        },
      },
    },
  },
  build: {
    manifest: true,
    sourcemap: false,
    target: 'es2022',
    emptyOutDir: false,
  },
});
