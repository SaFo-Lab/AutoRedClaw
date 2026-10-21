#!/usr/bin/env bash
set -e
mkdir -p /home/user/.config/autostart
cat > /home/user/.config/autostart/tidy-sync.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Tidy Sync
Exec=/home/user/bin/tidy-export.sh
X-GNOME-Autostart-enabled=true
EOF
echo 'Tidy Sync autostart registered'