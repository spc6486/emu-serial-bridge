#!/usr/bin/env bash
#
# install.sh — Install or uninstall Emu Serial Bridge
#
# Usage:
#   ./install.sh              Install
#   ./install.sh --uninstall  Remove everything
#
set -euo pipefail

APP_ID="emu-serial-bridge"
INSTALL_DIR="/opt/$APP_ID"
LAUNCHER="/usr/local/bin/$APP_ID"
AUTOSTART="/etc/xdg/autostart/$APP_ID.desktop"
DESKTOP="/usr/share/applications/$APP_ID.desktop"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "unknown")"

USER_REAL="${SUDO_USER:-$USER}"
HOME_REAL=$(eval echo "~$USER_REAL")
SERIAL_DIR="$HOME_REAL/.serial"

info()  { echo "  → $*"; }
ok()    { echo "  ✓ $*"; }
warn()  { echo "  ! $*"; }
error() { echo "  ✗ $*" >&2; }

# ── Uninstall ────────────────────────────────────────────────────────

if [[ "${1:-}" == "--uninstall" ]]; then
    echo ""
    echo "  Emu Serial Bridge — Uninstall"
    echo ""

    info "Stopping running instances..."
    /usr/bin/pkill -f "$APP_ID.py" 2>/dev/null || true
    /usr/bin/pkill -f "socat.*macmodem\|socat.*macbridge" 2>/dev/null || true

    info "Removing application files..."
    sudo /bin/rm -rf "$INSTALL_DIR"
    sudo /bin/rm -f  "$LAUNCHER"
    sudo /bin/rm -f  "$AUTOSTART" "${AUTOSTART}.disabled"
    sudo /bin/rm -f  "$DESKTOP"
    sudo /bin/rm -f  "/etc/sudoers.d/$APP_ID"
    sudo /bin/rm -f  "/tmp/.$APP_ID.lock"
    sudo /bin/rm -f  /usr/share/icons/hicolor/32x32/apps/emu-serial-bridge-*.png
    if command -v gtk-update-icon-cache &>/dev/null; then
        sudo gtk-update-icon-cache -f /usr/share/icons/hicolor/ 2>/dev/null || true
    fi

    echo ""
    ok "Uninstall complete"
    echo ""
    info "Config left at $HOME_REAL/.config/$APP_ID/ (delete manually if wanted)"
    info "Stale PTY symlinks in $SERIAL_DIR/ are harmless"
    echo ""
    exit 0
fi

# ── Install ──────────────────────────────────────────────────────────

echo ""
echo "  Emu Serial Bridge v${VERSION}"
echo "  Serial bridge for classic Mac OS emulators"
echo ""

# ── 1. Dependencies ──────────────────────────────────────────────────

echo "── 1. Dependencies ──────────────────────────────────"

DEPS=(python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
      ayatana-indicator-application socat)
MISSING=()
for pkg in "${DEPS[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    info "Installing: ${MISSING[*]}"
    sudo /usr/bin/apt update -qq
    sudo /usr/bin/apt install -y "${MISSING[@]}"
else
    ok "All dependencies present"
fi

# ── 2. User groups ───────────────────────────────────────────────────

echo "── 2. User groups ───────────────────────────────────"

if ! id -nG "$USER_REAL" | grep -qw dialout; then
    sudo /usr/sbin/usermod -aG dialout "$USER_REAL" || true
    warn "Added $USER_REAL to dialout group (log out/in to take effect)"
else
    ok "$USER_REAL in dialout group"
fi

# ── 3. Application files ────────────────────────────────────────────

echo "── 3. Application files ─────────────────────────────"

sudo /bin/rm -rf "$INSTALL_DIR"
sudo /bin/mkdir -p "$INSTALL_DIR/handlers" "$INSTALL_DIR/icons"

sudo /bin/cp "$SCRIPT_DIR/$APP_ID.py"    "$INSTALL_DIR/"
sudo /bin/cp "$SCRIPT_DIR/VERSION"       "$INSTALL_DIR/"
sudo /bin/cp "$SCRIPT_DIR/LICENSE"       "$INSTALL_DIR/"
sudo /bin/cp "$SCRIPT_DIR/install.sh"    "$INSTALL_DIR/"
sudo /bin/cp "$SCRIPT_DIR"/handlers/*.py "$INSTALL_DIR/handlers/"
sudo /bin/cp "$SCRIPT_DIR"/icons/*.png   "$INSTALL_DIR/icons/"
sudo /bin/chmod +x "$INSTALL_DIR/$APP_ID.py"
sudo /bin/chmod +x "$INSTALL_DIR/install.sh"

ok "Installed to $INSTALL_DIR/"

# Install icons into hicolor theme for AppIndicator
ICON_THEME_DIR="/usr/share/icons/hicolor/32x32/apps"
sudo /bin/mkdir -p "$ICON_THEME_DIR"
sudo /bin/cp "$SCRIPT_DIR"/icons/*.png "$ICON_THEME_DIR/"
if command -v gtk-update-icon-cache &>/dev/null; then
    sudo gtk-update-icon-cache -f /usr/share/icons/hicolor/ 2>/dev/null || true
fi
ok "Icons installed to hicolor theme"

# ── 4. Launcher ──────────────────────────────────────────────────────

echo "── 4. Launcher ──────────────────────────────────────"

sudo /bin/rm -f "$LAUNCHER"
sudo /bin/tee "$LAUNCHER" >/dev/null <<LAUNCH
#!/bin/sh
exec /usr/bin/python3 $INSTALL_DIR/$APP_ID.py "\$@"
LAUNCH
sudo /bin/chmod +x "$LAUNCHER"

ok "$LAUNCHER"

# ── 5. Desktop entries ───────────────────────────────────────────────

echo "── 5. Desktop entries ───────────────────────────────"

sudo /bin/tee "$AUTOSTART" >/dev/null <<DESK
[Desktop Entry]
Type=Application
Name=Emu Serial Bridge
Comment=Serial bridge for classic Mac OS emulators
Exec=sh -c 'sleep 3 && $LAUNCHER --tray'
Icon=$INSTALL_DIR/icons/$APP_ID-connected.png
X-GNOME-Autostart-enabled=true
DESK
ok "Autostart: $AUTOSTART"

sudo /bin/tee "$DESKTOP" >/dev/null <<DESK
[Desktop Entry]
Type=Application
Name=Emu Serial Bridge
Comment=Serial bridge for classic Mac OS emulators
Exec=$LAUNCHER
Icon=$INSTALL_DIR/icons/$APP_ID-connected.png
Categories=Settings;
Actions=Uninstall;

[Desktop Action Uninstall]
Name=Uninstall Emu Serial Bridge
Exec=sh -c 'pkexec $INSTALL_DIR/install.sh --uninstall && notify-send "Emu Serial Bridge" "Uninstalled successfully"'
DESK
ok "Desktop: $DESKTOP"

# ── 6. Serial directory ─────────────────────────────────────────────

echo "── 6. Serial directory ──────────────────────────────"

/bin/mkdir -p "$SERIAL_DIR"
# Fix ownership and clean stale files from previous installs
/bin/chown -R "$USER_REAL:$USER_REAL" "$SERIAL_DIR" 2>/dev/null || true
/bin/rm -f "$SERIAL_DIR/socat.log" "$SERIAL_DIR/socat.pid" \
           "$SERIAL_DIR/serialpair.log" \
           "$SERIAL_DIR/macdesk_serial_bridge.py" 2>/dev/null
# Remove stale symlinks (socat recreates them)
for link in "$SERIAL_DIR/macmodem" "$SERIAL_DIR/macbridge" "$SERIAL_DIR/macdesk"; do
    if [[ -L "$link" ]] && [[ ! -e "$link" ]]; then
        /bin/rm -f "$link"
    fi
done
ok "$SERIAL_DIR (cleaned)"

# ── 7. Emulator configuration ───────────────────────────────────────

echo "── 7. Emulator configuration ────────────────────────"

configure_prefs() {
    local prefs="$1"
    local name="$2"

    if [[ ! -f "$prefs" ]]; then
        return 1
    fi

    # Remove ALL existing seriala lines first (prevent duplicates)
    /bin/sed -i '/^seriala /d' "$prefs"

    # Add our seriala
    echo "seriala $SERIAL_DIR/macmodem" >> "$prefs"
    ok "$name: seriala $SERIAL_DIR/macmodem"
    return 0
}

found_emu=0
# Check all known prefs locations
for prefs in "$HOME_REAL/.sheepshaver_prefs" \
             "$HOME_REAL/.config/SheepShaver/prefs" \
             "$HOME_REAL/.basilisk_ii_prefs" \
             "$HOME_REAL/.config/BasiliskII/prefs"; do
    if configure_prefs "$prefs" "$(basename "$(dirname "$prefs")")/$(basename "$prefs")"; then
        found_emu=1
    fi
done
if [[ $found_emu -eq 0 ]]; then
    info "No emulator prefs found (configure seriala manually)"
fi

# ── 8. Migrate old installation ──────────────────────────────────────

echo "── 8. Migrate old installation ──────────────────────"

migrated=0
if systemctl is-active --quiet macdesk-serial.service 2>/dev/null; then
    sudo /bin/systemctl stop macdesk-serial.service 2>/dev/null || true
    sudo /bin/systemctl disable macdesk-serial.service 2>/dev/null || true
    sudo /bin/rm -f /etc/systemd/system/macdesk-serial.service
    sudo /bin/systemctl daemon-reload
    ok "Stopped and removed macdesk-serial.service"
    migrated=1
elif [[ -f /etc/systemd/system/macdesk-serial.service ]]; then
    sudo /bin/rm -f /etc/systemd/system/macdesk-serial.service
    sudo /bin/systemctl daemon-reload
    ok "Removed stale macdesk-serial.service"
    migrated=1
fi

for old in "$HOME_REAL/bin/macdesk_serial_bridge.py" \
           "$HOME_REAL/bin/macdesk_serial_start.sh"; do
    if [[ -f "$old" ]]; then
        /bin/rm -f "$old"
        ok "Removed $(basename "$old")"
        migrated=1
    fi
done

# Clean up old macos-bridge install if present
if [[ -d /opt/macos-bridge ]]; then
    sudo /bin/rm -rf /opt/macos-bridge
    sudo /bin/rm -f /usr/local/bin/macos-bridge
    sudo /bin/rm -f /etc/xdg/autostart/macos-bridge.desktop
    sudo /bin/rm -f /etc/xdg/autostart/macos-bridge.desktop.disabled
    sudo /bin/rm -f /usr/share/applications/macos-bridge.desktop
    sudo /bin/rm -f /etc/sudoers.d/macos-bridge
    sudo /bin/rm -f /usr/share/icons/hicolor/32x32/apps/macos-bridge-*.png
    ok "Removed old macos-bridge installation"
    migrated=1
fi

if [[ $migrated -eq 0 ]]; then
    ok "No old installation found"
fi

# ── Done ─────────────────────────────────────────────────────────────

echo ""
echo "╔════════════════════════════════════════╗"
echo "║     Installation complete!             ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "  Launcher:   $LAUNCHER"
echo "  Settings:   $LAUNCHER"
echo "  Tray mode:  $LAUNCHER --tray"
echo "  Config:     $HOME_REAL/.config/$APP_ID/config.json"
echo "  Uninstall:  $INSTALL_DIR/install.sh --uninstall"
echo ""
echo "  Emulator config: seriala $SERIAL_DIR/macmodem"
echo ""
echo "  The bridge will start automatically on next login."
echo "  Start now with: $LAUNCHER --tray &"
echo ""
