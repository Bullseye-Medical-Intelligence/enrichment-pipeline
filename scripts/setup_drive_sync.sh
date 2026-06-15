#!/usr/bin/env bash
# setup_drive_sync.sh
# Run this ONCE on each machine where you run the pipeline.
# It installs rclone and authorizes Google Drive access.
# After this, every pipeline run will auto-upload output/ to gdrive:BEMI-Runs.

set -e

REMOTE_NAME="gdrive"
DRIVE_FOLDER="BEMI-Runs"

echo ""
echo "===== Bullseye Drive Sync Setup ====="
echo ""

# 1. Install rclone if not present
if command -v rclone &>/dev/null; then
    echo "[OK] rclone already installed: $(rclone --version | head -1)"
else
    echo "[INSTALL] rclone not found. Installing..."
    curl https://rclone.org/install.sh | sudo bash
    echo "[OK] rclone installed."
fi

echo ""

# 2. Check if gdrive remote is already configured
if rclone listremotes | grep -q "^${REMOTE_NAME}:"; then
    echo "[OK] rclone remote '${REMOTE_NAME}' already configured."
else
    echo "[CONFIG] Setting up Google Drive remote '${REMOTE_NAME}'..."
    echo ""
    echo "  A browser window will open for Google authorization."
    echo "  Sign in with your Bullseye Google account."
    echo "  When asked for Drive scope, choose full access."
    echo ""
    rclone config create "${REMOTE_NAME}" drive scope drive
fi

echo ""

# 3. Test the connection by listing the root
echo "[TEST] Verifying Drive access..."
if rclone lsd "${REMOTE_NAME}:" &>/dev/null; then
    echo "[OK] Google Drive connected successfully."
else
    echo "[FAIL] Could not connect to Drive. Re-run this script or check your credentials."
    exit 1
fi

# 4. Create the BEMI-Runs folder if it doesn't exist
echo "[SETUP] Ensuring '${DRIVE_FOLDER}' folder exists on Drive..."
rclone mkdir "${REMOTE_NAME}:${DRIVE_FOLDER}" 2>/dev/null || true
echo "[OK] Folder ready: gdrive:${DRIVE_FOLDER}"

echo ""
echo "===== Setup complete ====="
echo ""
echo "Every pipeline run will now auto-upload output/ to:"
echo "  gdrive:${DRIVE_FOLDER}"
echo ""
echo "To manually sync at any time:"
echo "  rclone copy ./output gdrive:${DRIVE_FOLDER} --exclude step4_checkpoint.ndjson --exclude progress.json"
echo ""
