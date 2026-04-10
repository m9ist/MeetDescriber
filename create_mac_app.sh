#!/bin/bash
# Создаёт MeetDescriber.app — macOS-бандл для запуска двойным кликом из Finder.
# Запуск: bash create_mac_app.sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$PROJECT_DIR/MeetDescriber.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- Info.plist ---
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>start</string>
    <key>CFBundleIdentifier</key>
    <string>com.meetdescriber.app</string>
    <key>CFBundleName</key>
    <string>MeetDescriber</string>
    <key>CFBundleDisplayName</key>
    <string>MeetDescriber</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>MeetDescriber needs microphone access to capture audio via BlackHole virtual device.</string>
</dict>
</plist>
PLIST

# --- Executable ---
cat > "$APP/Contents/MacOS/start" << LAUNCHER
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$PROJECT_DIR"
.venv/bin/python -m app.main 2>/dev/null
LAUNCHER
chmod +x "$APP/Contents/MacOS/start"

echo "✓ Создан $APP"
echo "  Двойной клик в Finder или: open $APP"
