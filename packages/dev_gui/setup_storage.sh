#!/bin/bash
# setup_storage.sh — Configure external storage permissions for SFM logging
#
# Usage:
#   sudo ./setup_storage.sh                    # Auto-detect mounted drive under /media/<user>/*
#   sudo ./setup_storage.sh /path/to/storage    # Use a specific storage path
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}ERROR: This script must be run as root (use sudo)${NC}"
    exit 1
fi

# The user who will actually run the GUI (works whether invoked via sudo or not)
GUI_USER=${SUDO_USER:-$(whoami)}
GUI_UID=$(id -u "$GUI_USER")
GUI_GID=$(id -g "$GUI_USER")

STORAGE_PATH="$1"

# Auto-detect: scan /media/<user>/* for a mounted drive if no path was given.
if [[ -z "$STORAGE_PATH" ]]; then
    MEDIA_ROOT="/media/$GUI_USER"
    echo -e "${YELLOW}No path given — scanning $MEDIA_ROOT for mounted drives...${NC}"
    if [[ -d "$MEDIA_ROOT" ]]; then
        for entry in "$MEDIA_ROOT"/*; do
            if [[ -d "$entry" ]] && mountpoint -q "$entry"; then
                STORAGE_PATH="$entry"
                break
            fi
        done
    fi
    if [[ -z "$STORAGE_PATH" ]]; then
        echo -e "${RED}ERROR: No mounted drive found under $MEDIA_ROOT${NC}"
        echo "Plug in / mount the external drive first, or pass its path explicitly:"
        echo "  sudo ./setup_storage.sh /media/$GUI_USER/<volume-label>"
        exit 1
    fi
    echo -e "${GREEN}✓ Found mounted drive: $STORAGE_PATH${NC}"
fi

LOG_DIR="${STORAGE_PATH}/sfm_logs"

echo -e "${YELLOW}Setting up external storage for SFM logging...${NC}"
echo "Storage path: $STORAGE_PATH"
echo "Log directory: $LOG_DIR"
echo "GUI user: $GUI_USER (UID: $GUI_UID, GID: $GUI_GID)"
echo ""

# Check if storage path exists and is actually mounted
if [[ ! -d "$STORAGE_PATH" ]]; then
    echo -e "${RED}ERROR: Storage path does not exist: $STORAGE_PATH${NC}"
    exit 1
fi
if ! mountpoint -q "$STORAGE_PATH"; then
    echo -e "${YELLOW}WARNING: $STORAGE_PATH does not look like a mount point.${NC}"
    echo "Continuing anyway, but double-check the drive is actually mounted there."
fi

# Create log directory if it doesn't exist
if [[ ! -d "$LOG_DIR" ]]; then
    echo -e "${YELLOW}Creating log directory: $LOG_DIR${NC}"
    mkdir -p "$LOG_DIR"
    echo -e "${GREEN}✓ Created directory${NC}"
else
    echo -e "${GREEN}✓ Log directory already exists${NC}"
fi

# FAT/exFAT filesystems (common for USB drives) ignore chown/chmod — ownership
# and permissions are fixed at mount time via uid=/gid=/fmask=/dmask= mount
# options. Detect the filesystem and act accordingly.
FSTYPE=$(findmnt -n -o FSTYPE --target "$STORAGE_PATH" 2>/dev/null || echo "unknown")
echo "Filesystem type: $FSTYPE"

case "$FSTYPE" in
    vfat|exfat|ntfs)
        echo -e "${YELLOW}$FSTYPE does not support POSIX ownership/permissions.${NC}"
        MOUNT_UID=$(findmnt -n -o OPTIONS --target "$STORAGE_PATH" | grep -oP 'uid=\K[0-9]+' || echo "")
        if [[ "$MOUNT_UID" == "$GUI_UID" ]]; then
            echo -e "${GREEN}✓ Drive is already mounted with uid=$GUI_UID — $GUI_USER already owns it.${NC}"
        else
            echo -e "${RED}✗ Drive is mounted without uid=$GUI_UID (found: uid=${MOUNT_UID:-none}).${NC}"
            echo "  Re-mount it with the correct owner, e.g.:"
            echo "    sudo umount $STORAGE_PATH"
            echo "    sudo mount -o uid=$GUI_UID,gid=$GUI_GID <device> $STORAGE_PATH"
            echo "  Or add matching options to /etc/fstab / udisks2 config for auto-mount."
        fi
        ;;
    *)
        echo -e "${YELLOW}Setting ownership to $GUI_USER:$GUI_USER...${NC}"
        chown -R "$GUI_UID:$GUI_GID" "$STORAGE_PATH"
        echo -e "${GREEN}✓ Ownership set successfully${NC}"

        echo -e "${YELLOW}Setting permissions...${NC}"
        find "$STORAGE_PATH" -type d -exec chmod 755 {} \;
        find "$STORAGE_PATH" -type f -exec chmod 644 {} \;
        echo -e "${GREEN}✓ Permissions set successfully${NC}"
        ;;
esac

# Verify write access as the target user (not root) — this is the real test.
echo -e "${YELLOW}Verifying write access as $GUI_USER...${NC}"
TEST_FILE="$LOG_DIR/.write_test"
if sudo -u "$GUI_USER" touch "$TEST_FILE" 2>/dev/null; then
    rm -f "$TEST_FILE"
    echo -e "${GREEN}✓ Write access verified for $GUI_USER${NC}"
else
    echo -e "${RED}✗ Write access failed for $GUI_USER${NC}"
    echo "See guidance above to fix ownership/mount options, then re-run this script."
    exit 1
fi

echo ""
echo -e "${GREEN}✓ External storage setup complete!${NC}"
echo ""
echo "The following directory is ready for SFM logs:"
echo "  $LOG_DIR"
echo ""
