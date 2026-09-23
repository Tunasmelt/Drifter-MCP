import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" keeps asset URLs relative, so the built site works from any path
// (GitHub Pages project site, a subfolder, or a local file server).
export default defineConfig({
  base: "./",
  plugins: [react()],
});
