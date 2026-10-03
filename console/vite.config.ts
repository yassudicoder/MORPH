import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the console on :5173 and forwards the WebSocket and API to the Python backend.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      "/api": "http://127.0.0.1:8000",
    },
  },
});
