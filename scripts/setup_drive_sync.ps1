# setup_drive_sync.ps1
# Run this ONCE in PowerShell on each Windows machine where you run the pipeline.
# It installs rclone and authorizes Google Drive access.
# After this, every pipeline run will auto-upload output/ to gdrive:BEMI-Runs.

$RemoteName = "gdrive"
$DriveFolder = "BEMI-Runs"

Write-Host ""
Write-Host "===== Bullseye Drive Sync Setup =====" -ForegroundColor Cyan
Write-Host ""

# 1. Check if rclone is already installed
$rcloneCmd = Get-Command rclone -ErrorAction SilentlyContinue
if ($rcloneCmd) {
    $version = & rclone --version 2>&1 | Select-Object -First 1
    Write-Host "[OK] rclone already installed: $version" -ForegroundColor Green
} else {
    Write-Host "[INSTALL] rclone not found. Installing via winget..." -ForegroundColor Yellow

    # Try winget first (built into Windows 10/11)
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        winget install Rclone.Rclone --silent
    } else {
        Write-Host ""
        Write-Host "winget not available. Please install rclone manually:" -ForegroundColor Red
        Write-Host "  1. Go to https://rclone.org/downloads/"
        Write-Host "  2. Download the Windows installer"
        Write-Host "  3. Run the installer, then re-run this script"
        exit 1
    }

    # Refresh PATH so rclone is findable in this session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")

    $rcloneCmd = Get-Command rclone -ErrorAction SilentlyContinue
    if (-not $rcloneCmd) {
        Write-Host "[ERROR] rclone installed but not on PATH. Restart PowerShell and re-run this script." -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] rclone installed." -ForegroundColor Green
}

Write-Host ""

# 2. Check if gdrive remote is already configured
$remotes = & rclone listremotes 2>&1
if ($remotes -match "^${RemoteName}:") {
    Write-Host "[OK] rclone remote '$RemoteName' already configured." -ForegroundColor Green
} else {
    Write-Host "[CONFIG] Setting up Google Drive remote '$RemoteName'..." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  A browser window will open for Google authorization."
    Write-Host "  Sign in with your Bullseye Google account."
    Write-Host "  When asked for Drive scope, choose full access."
    Write-Host ""
    & rclone config create $RemoteName drive scope drive
}

Write-Host ""

# 3. Test the connection
Write-Host "[TEST] Verifying Drive access..." -ForegroundColor Yellow
$testResult = & rclone lsd "${RemoteName}:" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "[OK] Google Drive connected successfully." -ForegroundColor Green
} else {
    Write-Host "[FAIL] Could not connect to Drive. Re-run this script or check your credentials." -ForegroundColor Red
    Write-Host $testResult
    exit 1
}

# 4. Create the BEMI-Runs folder if it doesn't exist
Write-Host "[SETUP] Ensuring '$DriveFolder' folder exists on Drive..." -ForegroundColor Yellow
& rclone mkdir "${RemoteName}:${DriveFolder}" 2>&1 | Out-Null
Write-Host "[OK] Folder ready: gdrive:$DriveFolder" -ForegroundColor Green

Write-Host ""
Write-Host "===== Setup complete =====" -ForegroundColor Cyan
Write-Host ""
Write-Host "Every pipeline run will now auto-upload output/ to:"
Write-Host "  gdrive:$DriveFolder"
Write-Host ""
Write-Host "To manually sync at any time, run:"
Write-Host "  rclone copy .\output gdrive:$DriveFolder --exclude step4_checkpoint.ndjson --exclude progress.json"
Write-Host ""
