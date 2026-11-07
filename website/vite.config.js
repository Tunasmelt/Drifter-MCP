import { defineConfig } from "vite";

// The site is a single static index.html (no framework). base "./" keeps asset URLs
// relative, so the built site works from any path (GitHub Pages project site, a
// subfolder, or a local file server).
export default defineConfig({
  base: "./",
});
