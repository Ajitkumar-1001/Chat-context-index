import { defineConfig } from 'astro/config';

export default defineConfig({
  // Prose-heavy page: keep HTML whitespace between inline elements (7.x default is 'jsx').
  compressHTML: true,
});
