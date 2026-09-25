#!/usr/bin/env bash
# Jarvis ISO Builder
# ====================
# Builds a bootable ISO image from the prepared ISO directory.
# Usage: build-iso.sh <iso_dir> <output_iso>

set -euo pipefail

ISO_DIR="${1:?Usage: build-iso.sh <iso_dir> <output_iso>}"
ISO_OUTPUT="${2:?Usage: build-iso.sh <iso_dir> <output_iso>}"

echo "[*] Jarvis ISO Builder"
echo "========================"

# Validate inputs
if [ ! -d "$ISO_DIR" ]; then
    echo "[!] ERROR: ISO directory not found: $ISO_DIR"
    exit 1
fi

if [ ! -f "$ISO_DIR/boot/initramfs.gz" ]; then
    echo "[!] WARNING: initramfs not found at $ISO_DIR/boot/initramfs.gz"
fi

# Method 1: Try grub-mkrescue (preferred)
if command -v grub-mkrescue &>/dev/null; then
    echo "[*] Using grub-mkrescue..."
    grub-mkrescue -o "$ISO_OUTPUT" "$ISO_DIR"
    echo "[+] ISO created: $ISO_OUTPUT"
    exit 0
fi

# Method 2: Try xorriso directly
if command -v xorriso &>/dev/null; then
    echo "[*] Using xorriso..."
    xorriso -as mkisofs \
        -R -J \
        -V "JARVIS" \
        -b boot/grub/i386-pc/eltorito.img \
        -no-emul-boot \
        -boot-load-size 4 \
        -boot-info-table \
        -o "$ISO_OUTPUT" \
        "$ISO_DIR"
    echo "[+] ISO created: $ISO_OUTPUT"
    exit 0
fi

# Method 3: Try mkisofs / genisoimage
for tool in mkisofs genisoimage; do
    if command -v "$tool" &>/dev/null; then
        echo "[*] Using $tool..."
        "$tool" \
            -R -J \
            -V "JARVIS" \
            -o "$ISO_OUTPUT" \
            "$ISO_DIR"
        echo "[+] ISO created: $ISO_OUTPUT"
        exit 0
    fi
done

# Method 4: Create a raw structure that can be used with dd
echo "[*] No ISO tools found. Creating raw bootable image..."
echo "[*] Installing with: apt-get install grub-mkrescue xorriso mtools"

# Create a minimal raw disk image as fallback
DISK_SIZE=64  # MB
dd if=/dev/zero of="$ISO_OUTPUT" bs=1M count=$DISK_SIZE status=progress 2>/dev/null || \
    dd if=/dev/zero of="$ISO_OUTPUT" bs=1M count=$DISK_SIZE

# Copy the contents into the image at a fixed offset
echo "[*] Embedding filesystem into raw image..."
INITRAMFS="$ISO_DIR/boot/initramfs.gz"
if [ -f "$INITRAMFS" ]; then
    # Write initramfs at 1MB offset
    dd if="$INITRAMFS" of="$ISO_OUTPUT" bs=1M seek=1 conv=notrunc 2>/dev/null
fi

echo "[+] Raw image created: $ISO_OUTPUT"
echo "[!] NOTE: This raw image requires a bootloader to be installed."
echo "    For a proper bootable ISO, install: grub-mkrescue xorriso"
echo ""
echo "    To make bootable with GRUB:"
echo "      losetup /dev/loop0 $ISO_OUTPUT"
echo "      grub-install --target=i386-pc --boot-directory=/mnt/boot /dev/loop0"
echo "      losetup -d /dev/loop0"
