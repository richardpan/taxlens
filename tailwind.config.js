// Tailwind CSS configuration for TaxLens.
// We tree-shake utility classes by scanning the web/index.html and web/app.js
// sources. The build runs offline (`npx tailwindcss -i ... -o ...`) and writes
// a minified, content-pruned stylesheet to src/taxlens/web/vendor/tailwind.css.
//
// Adding a new utility class anywhere in index.html or app.js? Re-run the
// build script (scripts/build-tailwind.ps1) to regenerate the vendored CSS.
module.exports = {
  content: [
    "./src/taxlens/web/index.html",
    "./src/taxlens/web/app.js",
  ],
  theme: { extend: {} },
  plugins: [],
};
