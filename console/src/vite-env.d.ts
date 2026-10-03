/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Set when the console is hosted apart from the backend (e.g. console on Vercel, brain on Render):
  // VITE_TOWER_BACKEND=https://tower-xxxx.onrender.com
  readonly VITE_TOWER_BACKEND?: string;
}
