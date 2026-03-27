r"""
Регистрирует Native Messaging хост в Windows реестре / Mac LaunchAgents.

Windows: HKCU\Software\Google\Chrome\NativeMessagingHosts\com.for_meets.host
Mac:     ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
"""

import io
import json
import platform
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


HOST_NAME = "com.for_meets.host"


def get_exe_path(host_script: str) -> str:
    """
    Chrome на Windows требует .exe — .bat не запускается через CreateProcess.
    Возвращает путь к скомпилированному for_meets_host.exe.
    """
    exe = Path(host_script).parent / "dist" / "for_meets_host" / "for_meets_host.exe"
    if not exe.exists():
        raise FileNotFoundError(
            f"for_meets_host.exe не найден: {exe}\n"
            "Собери его: python -m PyInstaller --onefile --name for_meets_host "
            "app/extension/native_host.py --distpath app/extension/dist"
        )
    return str(exe)


def get_host_manifest(python_exe: str, host_script: str) -> dict:
    if platform.system() == "Windows":
        path = get_exe_path(host_script)
    else:
        path = python_exe
    return {
        "name": HOST_NAME,
        "description": "for_meets Native Messaging Host",
        "path": path,
        "type": "stdio",
        "allowed_origins": [],
    }


def get_extension_id() -> str | None:
    """Читает Extension ID из сохранённого файла (записывается после установки)."""
    id_file = Path(__file__).parent / "extension_id.txt"
    if id_file.exists():
        return id_file.read_text().strip()
    return None


def install_windows(python_exe: str, host_script: str, extension_id: str | None) -> bool:
    try:
        import winreg
    except ImportError:
        print("  ✗  winreg недоступен — не Windows?")
        return False

    manifest = get_host_manifest(python_exe, host_script)
    if extension_id:
        manifest["allowed_origins"] = [f"chrome-extension://{extension_id}/"]
    else:
        print("  !  Extension ID не найден — allowed_origins пуст, хост не будет принят Chrome")
        print("     После установки расширения запусти: python -m app.extension.install_host --update-id <ID>")

    # Сохраняем manifest.json рядом со скриптом
    manifest_path = Path(__file__).parent / "com.for_meets.host.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Прописываем в реестр
    reg_key = r"Software\Google\Chrome\NativeMessagingHosts\com.for_meets.host"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_key) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
        print(f"  ✓  Хост зарегистрирован: {manifest_path}")
        return True
    except Exception as e:
        print(f"  ✗  Ошибка реестра: {e}")
        return False


def install_mac(python_exe: str, host_script: str, extension_id: str | None) -> bool:
    manifest = get_host_manifest(python_exe, host_script)
    if extension_id:
        manifest["allowed_origins"] = [f"chrome-extension://{extension_id}/"]

    host_dir = Path.home() / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
    host_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = host_dir / "com.for_meets.host.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  ✓  Хост зарегистрирован: {manifest_path}")
    return True


def install(extension_id: str | None = None) -> bool:
    python_exe = sys.executable
    host_script = str(Path(__file__).parent / "native_host.py")

    system = platform.system()
    if system == "Windows":
        return install_windows(python_exe, host_script, extension_id)
    elif system == "Darwin":
        return install_mac(python_exe, host_script, extension_id)
    else:
        print(f"  ✗  Платформа {system} не поддерживается")
        return False


def update_extension_id(extension_id: str) -> None:
    """Обновляет Extension ID в manifest после установки расширения."""
    id_file = Path(__file__).parent / "extension_id.txt"
    id_file.write_text(extension_id.strip())
    install(extension_id=extension_id)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-id", help="Обновить Extension ID и перерегистрировать хост")
    args = parser.parse_args()

    if args.update_id:
        update_extension_id(args.update_id)
    else:
        ext_id = get_extension_id()
        install(extension_id=ext_id)
