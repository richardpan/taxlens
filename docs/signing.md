# Signing & notarizing the TaxLens desktop app

The Electron build (`npm --prefix ./desktop run dist`) is **unsigned by default**.
Distributing unsigned binaries triggers SmartScreen / Gatekeeper warnings.

## Windows (EV or OV code-signing certificate)

```powershell
$env:CSC_LINK = "C:\path\to\cert.pfx"
$env:CSC_KEY_PASSWORD = "<pfx-password>"
npm --prefix .\desktop run dist
```

For HSM-backed EV certs:

```powershell
signtool sign /tr http://timestamp.digicert.com /td SHA256 /fd SHA256 `
  /a /n "Your Org Name" `
  desktop\dist\TaxLens-Setup-0.3.0.exe
```

## macOS (Developer ID + notarization)

```bash
export CSC_LINK="$HOME/Certificates/DeveloperID.p12"
export CSC_KEY_PASSWORD='<p12-password>'
export APPLE_ID='you@example.com'
export APPLE_APP_SPECIFIC_PASSWORD='abcd-efgh-ijkl-mnop'
export APPLE_TEAM_ID='ABCDE12345'

npm --prefix ./desktop run dist          # electron-builder handles notarization

# Or by hand:
xcrun notarytool submit desktop/dist/TaxLens-0.3.0.dmg \
  --apple-id "$APPLE_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" \
  --team-id "$APPLE_TEAM_ID" --wait
xcrun stapler staple desktop/dist/TaxLens-0.3.0.dmg
```

## Linux

We ship an AppImage. Most distros don't require signing; we publish a SHA-256
sum alongside each GitHub release for users to verify.

## CI

Store as GitHub Actions secrets:
- `WIN_CSC_LINK` (base64-encoded .pfx) + `WIN_CSC_KEY_PASSWORD`
- `MAC_CSC_LINK` + `MAC_CSC_KEY_PASSWORD`
- `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`

## Reproducible builds

`desktop/bin/taxlens-backend.exe` is produced by
`desktop/scripts/build-backend.ps1` (PyInstaller). It embeds the Python
interpreter and all `tax_rules/`, `src/taxlens/web/`, and demo YAMLs.
