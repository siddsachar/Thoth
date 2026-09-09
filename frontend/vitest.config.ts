import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    // Approved test dependency only: never load an OpenTelemetry SDK/exporter.
    experimental: { openTelemetry: { enabled: false } },
    api: false,
    watch: false,
    browser: { enabled: false },
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'tests/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    clearMocks: true,
  },
});
