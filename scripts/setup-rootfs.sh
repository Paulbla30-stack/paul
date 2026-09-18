#!/usr/bin/env bash
# Jarvis Root Filesystem Setup
# ===============================
# Prepares a complete root filesystem for the bootable ISO.
# This script is run on the build host to populate the rootfs.

set -euo pipefail

ROOTFS="${1:?Usage: setup-rootfs.sh <rootfs_dir>}"

echo "[*] Setting up Jarvis root filesystem at: $ROOTFS"

# ---- Create directory structure ----
echo "[*] Creating directory structure..."
mkdir -p "$ROOTFS"/{bin,sbin,lib,lib64,etc,proc,sys,dev,tmp,var,run,root,home}
mkdir -p "$ROOTFS"/usr/{bin,sbin,lib,local/bin,local/lib,lib/jarvis}
mkdir -p "$ROOTFS"/etc/{jarvis,init.d,network}
mkdir -p "$ROOTFS"/var/{log,run,tmp}

# ---- Install busybox (static) ----
echo "[*] Installing busybox..."
if command -v busybox &>/dev/null; then
    BUSYBOX=$(command -v busybox)
    if file "$BUSYBOX" | grep -q "statically linked"; then
        cp "$BUSYBOX" "$ROOTFS/bin/busybox"
    else
        echo "[!] WARNING: busybox is dynamically linked, copying with libraries"
        cp "$BUSYBOX" "$ROOTFS/bin/busybox"
        # Copy required libraries
        ldd "$BUSYBOX" 2>/dev/null | grep -oP '/\S+' | while read -r lib; do
            dir=$(dirname "$lib")
            mkdir -p "$ROOTFS$dir"
            cp "$lib" "$ROOTFS$lib" 2>/dev/null || true
        done
    fi
    chmod +x "$ROOTFS/bin/busybox"

    # Create busybox symlinks
    cd "$ROOTFS/bin"
    for cmd in sh ash ls cp mv rm mkdir cat mount umount grep sed awk \
               ps kill top free df du ip ifconfig ping hostname \
               chmod chown ln tar gzip gunzip vi; do
        ln -sf busybox "$cmd" 2>/dev/null || true
    done
    cd - >/dev/null
else
    echo "[!] WARNING: busybox not found, shell utilities will be limited"
fi

# ---- Install Python 3 ----
echo "[*] Installing Python 3..."
PYTHON_BIN=$(command -v python3 2>/dev/null || true)
if [ -n "$PYTHON_BIN" ]; then
    cp "$PYTHON_BIN" "$ROOTFS/usr/bin/python3"
    ln -sf python3 "$ROOTFS/usr/bin/python"

    # Copy Python standard library
    PYTHON_LIB=$(python3 -c "import sys; print(sys.prefix)")/lib
    if [ -d "$PYTHON_LIB" ]; then
        cp -r "$PYTHON_LIB"/python3* "$ROOTFS/usr/lib/" 2>/dev/null || true
    fi

    # Copy shared libraries needed by Python
    ldd "$PYTHON_BIN" 2>/dev/null | grep -oP '/\S+' | while read -r lib; do
        dir=$(dirname "$lib")
        mkdir -p "$ROOTFS$dir"
        cp "$lib" "$ROOTFS$lib" 2>/dev/null || true
    done
else
    echo "[!] WARNING: python3 not found, agent will not work"
fi

# ---- Set up /etc files ----
echo "[*] Setting up /etc..."

# /etc/passwd
cat > "$ROOTFS/etc/passwd" << 'PASSWD'
root:x:0:0:root:/root:/bin/sh
jarvis:x:1000:1000:Jarvis Agent:/home/jarvis:/bin/sh
PASSWD

# /etc/group
cat > "$ROOTFS/etc/group" << 'GROUP'
root:x:0:
input:x:5:jarvis
video:x:44:jarvis
disk:x:6:jarvis
jarvis:x:1000:
GROUP

# /etc/fstab
cat > "$ROOTFS/etc/fstab" << 'FSTAB'
proc            /proc        proc    defaults    0 0
sysfs           /sys         sysfs   defaults    0 0
devtmpfs        /dev         devtmpfs defaults   0 0
tmpfs           /tmp         tmpfs   defaults    0 0
tmpfs           /var         tmpfs   defaults    0 0
FSTAB

# /etc/hostname
echo "jarvis" > "$ROOTFS/etc/hostname"

echo "[+] Root filesystem setup complete: $ROOTFS"
echo ""
echo "Contents:"
find "$ROOTFS" -maxdepth 3 -type f | head -50
echo "..."
echo "Total files: $(find "$ROOTFS" -type f | wc -l)"
echo "Total size:  $(du -sh "$ROOTFS" | cut -f1)"
