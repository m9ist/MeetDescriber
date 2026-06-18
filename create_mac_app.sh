#!/bin/bash
# Создаёт MeetDescriber.app — macOS-бандл для запуска двойным кликом из Finder.
# Также собирает audiotee (Core Audio Tap CLI) и бандлит в Resources/.
# Запуск: bash create_mac_app.sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$PROJECT_DIR/MeetDescriber.app"
AUDIOTEE_SRC="$PROJECT_DIR/vendor/audiotee"
AUDIOTEE_BIN="$AUDIOTEE_SRC/.build/release/audiotee"

# --- Сборка audiotee (если нужно) ---
if [ ! -f "$AUDIOTEE_BIN" ] || [ "$AUDIOTEE_SRC/Package.swift" -nt "$AUDIOTEE_BIN" ]; then
    echo "Собираю audiotee..."
    if ! command -v swift > /dev/null; then
        echo "✗ swift не найден. Установи Xcode Command Line Tools: xcode-select --install"
        exit 1
    fi
    (cd "$AUDIOTEE_SRC" && swift build -c release)
fi
if [ ! -f "$AUDIOTEE_BIN" ]; then
    echo "✗ audiotee не собрался: $AUDIOTEE_BIN"
    exit 1
fi

# --- Структура бандла ---
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
    <string>MeetDescriber записывает звук с микрофона во время встреч.</string>
    <key>NSAudioCaptureUsageDescription</key>
    <string>MeetDescriber записывает системный звук (голоса собеседников из браузера) через Core Audio Tap.</string>
</dict>
</plist>
PLIST

# --- audiotee бинарь ---
cp "$AUDIOTEE_BIN" "$APP/Contents/Resources/audiotee"
chmod +x "$APP/Contents/Resources/audiotee"

# --- Launcher ---
cat > "$APP/Contents/MacOS/start" << LAUNCHER
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$PROJECT_DIR"
.venv/bin/python -m app.main 2>/dev/null
LAUNCHER
chmod +x "$APP/Contents/MacOS/start"

# --- Ad-hoc signing — иначе macOS не даёт NSAudioCaptureUsageDescription ---
codesign --force --sign - "$APP/Contents/Resources/audiotee" 2>/dev/null || true
codesign --force --sign - "$APP" 2>/dev/null || true

echo "✓ Создан $APP"
echo "  audiotee:    $APP/Contents/Resources/audiotee"
echo "  Двойной клик в Finder или: open $APP"
