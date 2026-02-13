# Simplifi WorthBar

Tiny macOS menu bar app that shows your Quicken Simplifi net worth.

## Features
- Polls your current net worth and updates menu bar label
- Onboarding status: clearly shows `Sign In` when Simplifi session is missing
- Display modes:
  - `Compact` (`$1.2M +2%`)
  - `Full` (`$1,234,567 +2%`)
  - `Delta Today` (`+$2.4K`)
- One-click `Copy Diagnostics` for support/debugging
- Runs in background via LaunchAgent

## Requirements
- macOS
- Python 3
- Active Simplifi web session available from MenubarX WebKit storage

## Install (Easiest)
1. Download `SimplifiWorthBar-macos.dmg` from Releases.
2. Open the DMG and drag `Simplifi WorthBar.app` into `Applications`.
3. Launch `Simplifi WorthBar.app`.

## Install (CLI)
```bash
./install.sh
```

## Uninstall
```bash
launchctl bootout "gui/$UID" "$HOME/Library/LaunchAgents/io.github.simplifi-worthbar.agent.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/io.github.simplifi-worthbar.agent.plist"
rm -rf "$HOME/Library/Application Support/SimplifiWorthBar"
```

## Build Locally
```bash
/usr/bin/swiftc -O -framework AppKit -framework Foundation -o SimplifiWorthBar SimplifiWorthBar.swift
```

## Build App + DMG Locally
```bash
chmod +x scripts/build_macos_artifacts.sh
VERSION=0.1.2 scripts/build_macos_artifacts.sh
```

By default, artifacts are unsigned. For smoother install on other machines, use Developer ID signing + notarization with:
- `CODESIGN_IDENTITY`
- `NOTARY_PROFILE`

GitHub release workflow can sign/notarize automatically when these repository secrets are set:
- `APPLE_CODESIGN_IDENTITY`
- `APPLE_DEVELOPER_ID_CERT_P12` (base64-encoded `.p12`)
- `APPLE_DEVELOPER_ID_CERT_PASSWORD`
- `APPLE_ID`
- `APPLE_TEAM_ID`
- `APPLE_APP_SPECIFIC_PASSWORD`

## Privacy
- Reads local token/session data from your local user profile
- Stores refreshed token cache at `~/Library/Application Support/SimplifiWorthBar/`
- Sends only required API requests to Simplifi endpoints to fetch account data

## License
MIT
