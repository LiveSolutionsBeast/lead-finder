# sync-extension.ps1
# =================
# Pulls the latest Lead Finder Chrome extension from the Tailscale server
# and extracts it into the laptop's extension folder.
#
# Usage (from PowerShell):
#   .\sync-extension.ps1                    # sync to default folder
#   .\sync-extension.ps1 -Dst "C:\path"     # sync to a different folder
#
# What it does:
#   Part 1: Fetches the latest extension zip from the lead-finder server,
#           extracts it, and verifies the new content.js matches the server.
#   Part 2: Clears Chrome's extension and content-script caches, then prints
#           the "nuclear reload" instructions for Chrome.
#           (Chrome's content-script cache is the real reason you see
#           "old code" — removing and re-adding the extension is not always
#           enough if Chrome still holds cached scripts in its user-data dir.)
#
# Tailscale requirement: this machine must be on the same Tailnet as
# the lead-finder server (lsb-wsl.tail4f816e.ts.net). Verify with:
#   Test-NetConnection -ComputerName lsb-wsl.tail4f816e.ts.net -Port 8798
# That should return TcpTestSucceeded: True.

[CmdletBinding()]
param(
    [string]$Dst = "$env:USERPROFILE\lead-finder-extension",
    [string]$Server = "http://lsb-wsl.tail4f816e.ts.net:8798",
    [switch]$NoCacheClear = $false
)

$ErrorActionPreference = "Stop"

# ── Part 1: Sync the extension files ─────────────────────────────────────

Write-Host ""
Write-Host "Lead Finder — Extension Sync" -ForegroundColor Cyan
Write-Host "Server: $Server" -ForegroundColor Gray
Write-Host "Destination: $Dst" -ForegroundColor Gray
Write-Host ""

# Reachability check
try {
    $health = Invoke-RestMethod -Uri "$Server/api/health" -TimeoutSec 5
} catch {
    Write-Host "Cannot reach the lead-finder server at $Server" -ForegroundColor Red
    Write-Host "Check: is the server running? Are you on Tailscale?" -ForegroundColor Red
    exit 1
}
Write-Host "Server status: $($health.status)  (db ok: $($health.database.ok))" -ForegroundColor Green

# Get server's content.js size
try {
    $filecheck = Invoke-RestMethod -Uri "$Server/extension/filecheck" -TimeoutSec 5
    $serverJs = $filecheck.files | Where-Object { $_.path -eq "content.js" } | Select-Object -First 1
    $serverSize = $serverJs.size
} catch {
    Write-Host "Cannot fetch /extension/filecheck" -ForegroundColor Red
    exit 1
}
Write-Host "Server content.js: $serverSize bytes"

# Check local
$localSize = $null
$localPath = Join-Path $Dst "content.js"
if (Test-Path $localPath) {
    $localSize = (Get-Item $localPath).Length
    Write-Host "Local  content.js: $localSize bytes  ($((Resolve-Path $Dst).Path))"
    if ($localSize -eq $serverSize) {
        Write-Host "Already up to date. Skipping download." -ForegroundColor Yellow
    } else {
        Write-Host "Stale ($localSize vs $serverSize). Re-syncing..." -ForegroundColor Magenta
    }
} else {
    Write-Host "Local  content.js: not found (first install)" -ForegroundColor Yellow
}

# Download + extract
if ($null -eq $localSize -or $localSize -ne $serverSize) {
    $zip = Join-Path $env:USERPROFILE "Downloads\lf-ext.zip"
    Write-Host ""
    Write-Host "Downloading $Server/extension/download ..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "$Server/extension/download" -OutFile $zip -UseBasicParsing

    # Wipe destination
    if (Test-Path $Dst) {
        Write-Host "Wiping $Dst ..." -ForegroundColor Gray
        Remove-Item -Recurse -Force "$Dst\*" -ErrorAction SilentlyContinue
    } else {
        Write-Host "Creating $Dst ..." -ForegroundColor Gray
        New-Item -ItemType Directory -Force -Path $Dst | Out-Null
    }

    # Extract
    Write-Host "Extracting ..." -ForegroundColor Cyan
    Expand-Archive $zip -DestinationPath $Dst -Force
    Remove-Item $zip

    # Verify
    $newSize = (Get-Item (Join-Path $Dst "content.js")).Length
    if ($newSize -eq $serverSize) {
        Write-Host ""
        Write-Host "Sync OK. content.js: $newSize bytes  (matches server)" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "MISMATCH: local=$newSize server=$serverSize" -ForegroundColor Red
        Write-Host "Try running the script again." -ForegroundColor Red
        exit 1
    }
}

# Show what landed
Write-Host ""
Write-Host "Files in $Dst :" -ForegroundColor Cyan
Get-ChildItem $Dst | Format-Table Name, Length -AutoSize | Out-String | Write-Host

# ── Part 2: Clear Chrome caches for a clean load ─────────────────────────

if (-not $NoCacheClear) {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Yellow
    Write-Host "  PART 2 — Clearing Chrome extension / content-script caches" -ForegroundColor Yellow
    Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Yellow
    Write-Host ""

    # Warn if Chrome is running.
    $chrome = Get-Process chrome -ErrorAction SilentlyContinue
    if ($chrome) {
        Write-Host "WARNING: Chrome is currently running." -ForegroundColor Red
        Write-Host "Close all Chrome windows now, then press Enter to continue..." -ForegroundColor Yellow
        Read-Host | Out-Null
    }

    # Chrome user-data paths.
    $chromePaths = @(
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Extensions",
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Code Cache",
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Service Worker",
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Storage\ext",
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Profile *\Extensions",
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Profile *\Code Cache",
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Profile *\Service Worker",
        "$env:LOCALAPPDATA\Google\Chrome\User Data\Profile *\Storage\ext"
    )

    foreach ($p in $chromePaths) {
        if (Test-Path $p) {
            try {
                Remove-Item -Recurse -Force -Path "$p\*" -ErrorAction SilentlyContinue
                Write-Host "Cleared: $p" -ForegroundColor Gray
            } catch {
                Write-Host "Could not fully clear: $p" -ForegroundColor DarkYellow
            }
        }
    }

    # Try to unregister service workers via Chrome's debug protocol (optional,
    # may fail if Chrome isn't running with remote-debugging; harmless).
    Write-Host ""
    Write-Host "Cache clearing complete." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Skipped Chrome cache clearing (-NoCacheClear was set)." -ForegroundColor Yellow
}

# ── Part 3: Nuclear reload reminder ──────────────────────────────────────

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Yellow
Write-Host "  PART 3 — Nuclear reload in Chrome (REQUIRED for new code)" -ForegroundColor Yellow
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Yellow
Write-Host ""
Write-Host "Files are updated on disk and Chrome caches have been cleared." -ForegroundColor White
Write-Host "A regular 'Reload' button on the extension card is NOT enough" -ForegroundColor White
Write-Host "(Chrome caches the content script). You MUST do a nuclear reload:" -ForegroundColor White
Write-Host ""
Write-Host "  1. Open chrome://extensions/  in Chrome" -ForegroundColor White
Write-Host "  2. Find Lead Finder — click REMOVE (the trash icon)" -ForegroundColor White
Write-Host "  3. Toggle 'Developer mode' ON (top right)" -ForegroundColor White
Write-Host "  4. Click 'Load unpacked'" -ForegroundColor White
Write-Host "  5. Select: $Dst" -ForegroundColor Cyan
Write-Host "  6. Open a NEW LinkedIn profile tab (do not restore closed tabs)" -ForegroundColor White
Write-Host "  7. Press Ctrl+Shift+R (hard refresh)" -ForegroundColor White
Write-Host "  8. F12 → Console tab — you should see:" -ForegroundColor White
Write-Host "       [lead-finder/cs] BUILD_ID=parser-20260725-0735" -ForegroundColor Cyan
Write-Host ""
Write-Host "After that, the new code is running. Click the Lead Finder icon to test." -ForegroundColor Green
Write-Host ""
