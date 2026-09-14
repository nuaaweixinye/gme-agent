import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const backendPort = Number(process.env.VITE_BACKEND_PORT || "8765");

export default defineConfig({
  base: "./",
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
});
