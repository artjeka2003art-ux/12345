# ghostoverlay/cli.py
import os
import subprocess
import sys
import time
import signal

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    daemon_path = os.path.join(base_dir, "..", "ghostcoach", "daemon.py")
    overlay_path = os.path.join(base_dir, "main.js")

    # 1. Запускаем демон GhostCoach
    print("👻 Запускаю GhostCoach демон...")
    daemon_proc = subprocess.Popen([sys.executable, daemon_path])

    # Немного ждём, чтобы сервер успел подняться
    time.sleep(1.5)

    # 2. Запускаем Electron Overlay (как отдельный процесс)
    print("✨ Запускаю GhostOverlay...")
    try:
        overlay_proc = subprocess.Popen(["npx", "electron", overlay_path])

        # Ждём только Electron, демон живёт сам
        overlay_proc.wait()
    finally:
        print("🛑 Останавливаю демон GhostCoach...")
        daemon_proc.send_signal(signal.SIGTERM)
        daemon_proc.wait()

if __name__ == "__main__":
    main()
