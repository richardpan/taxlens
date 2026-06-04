# Builds the vendored, tree-shaken Tailwind CSS used by the TaxLens web UI.
#
# Replaces the legacy 440 KB runtime tailwind.js (which compiled CSS in the
# browser) with a ~17 KB pre-built tailwind.css. Re-run any time you add new
# utility classes in src/taxlens/web/index.html or src/taxlens/web/app.js —
# unused classes are pruned by content scanning (see tailwind.config.js).
$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path "$PSScriptRoot\.."
Set-Location $repoRoot

$out = "src/taxlens/web/vendor/tailwind.css"
& npx -y -p tailwindcss@3 tailwindcss `
    -c tailwind.config.js `
    -i scripts/tailwind.input.css `
    -o $out `
    --minify

if (-not (Test-Path $out)) { throw "Tailwind build failed: $out not produced." }
$bytes = (Get-Item $out).Length
Write-Host ("Wrote {0} ({1:N1} KB)" -f $out, ($bytes / 1KB))
