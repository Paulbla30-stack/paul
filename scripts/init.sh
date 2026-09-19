#!/bin/sh
# Jarvis Init Script
# ====================
# This is the first process (PID 1) that runs when the ISO boots.
# It sets up the environment and launches the Jarvis agent.

echo "=========================================="
echo "  Jarvis Agentic Agent Environment"
echo "  Version 1.0.0"
echo "=========================================="

# ---- Mount essential filesystems ----
echo "[init] Mounting essential filesystems..."
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
mount -t devtmpfs devtmpfs /dev 2>/dev/null
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts 2>/dev/null
mount -t tmpfs tmpfs /dev/shm 2>/dev/null
mount -t tmpfs tmpfs /tmp 2>/dev/null
mount -t tmpfs tmpfs /var 2>/dev/null
mkdir -p /var/log /var/run /var/tmp

# ---- Parse kernel command line ----
AUTOSTART=1
FULLACCESS=1
SAFEMODE=0
SCANONLY=0
DEBUG=0

for param in $(cat /proc/cmdline 2>/dev/null); do
    case "$param" in
        jarvis.autostart=*) AUTOSTART="${param#*=}" ;;
        jarvis.fullaccess=*) FULLACCESS="${param#*=}" ;;
        jarvis.safemode=*)  SAFEMODE="${param#*=}" ;;
        jarvis.scanonly=*)  SCANONLY="${param#*=}" ;;
        jarvis.debug=*)     DEBUG="${param#*=}" ;;
    esac
done

echo "[init] Configuration:"
echo "       autostart=$AUTOSTART fullaccess=$FULLACCESS"
echo "       safemode=$SAFEMODE scanonly=$SCANONLY debug=$DEBUG"

# ---- Set up hardware access ----
if [ "$FULLACCESS" = "1" ] && [ "$SAFEMODE" = "0" ]; then
    echo "[init] Enabling full hardware access..."

    # Framebuffer (screen)
    if [ -e /dev/fb0 ]; then
        chmod 666 /dev/fb0
        echo "[init]   Screen:  /dev/fb0 - READY"
    else
        echo "[init]   Screen:  /dev/fb0 - NOT FOUND (no framebuffer)"
    fi

    # Input devices (keyboard, mouse)
    chmod 666 /dev/input/* 2>/dev/null
    if [ -e /dev/input/mice ]; then
        echo "[init]   Mouse:   /dev/input/mice - READY"
    fi
    for ev in /dev/input/event*; do
        [ -e "$ev" ] && echo "[init]   Input:   $ev - READY"
    done

    # Video memory via /dev/mem
    if [ -e /dev/mem ]; then
        chmod 660 /dev/mem
        echo "[init]   VideoMem:/dev/mem - READY"
    fi

    # DRM devices
    for drm in /dev/dri/*; do
        if [ -e "$drm" ]; then
            chmod 666 "$drm"
            echo "[init]   DRM:     $drm - READY"
        fi
    done

    # Storage devices
    for dev in /dev/sd* /dev/nvme* /dev/vd* /dev/hd*; do
        if [ -e "$dev" ]; then
            echo "[init]   Storage: $dev - AVAILABLE"
        fi
    done

    # RAM info
    if [ -f /proc/meminfo ]; then
        TOTAL_MEM=$(grep MemTotal /proc/meminfo | awk '{print $2}')
        echo "[init]   RAM:     ${TOTAL_MEM}kB total - ACCESSIBLE"
    fi
else
    echo "[init] Running in safe mode - restricted hardware access"
fi

# ---- Set hostname ----
echo "jarvis" > /proc/sys/kernel/hostname 2>/dev/null
echo "[init] Hostname: jarvis"

# ---- Set up networking (basic) ----
ip link set lo up 2>/dev/null
for iface in eth0 ens0 enp0s3; do
    if ip link show "$iface" &>/dev/null; then
        ip link set "$iface" up 2>/dev/null
        echo "[init] Network interface $iface brought up"
    fi
done

# ---- Launch Jarvis ----
if [ "$AUTOSTART" = "1" ]; then
    echo ""
    echo "[init] Starting Jarvis Agent..."
    echo ""

    export JARVIS_FULLACCESS="$FULLACCESS"
    export JARVIS_SAFEMODE="$SAFEMODE"
    export JARVIS_SCANONLY="$SCANONLY"
    export JARVIS_DEBUG="$DEBUG"
    export PYTHONPATH="/usr/lib/jarvis"

    if [ "$SCANONLY" = "1" ]; then
        echo "[init] Running security scan only..."
        python3 /usr/lib/jarvis/jarvis/security/scanner.py --target / --report /tmp/scan-report.txt
        echo ""
        echo "[init] Scan complete. Report at /tmp/scan-report.txt"
        cat /tmp/scan-report.txt
    else
        python3 /usr/lib/jarvis/jarvis/main.py
    fi
fi

# ---- Drop to shell if agent exits ----
echo ""
echo "[init] Jarvis agent has stopped."
echo "[init] Dropping to maintenance shell..."
echo ""

# Try to give a usable shell
if [ -x /bin/bash ]; then
    exec /bin/bash
elif [ -x /bin/sh ]; then
    exec /bin/sh
else
    # Busybox fallback
    exec /bin/sh
fi
