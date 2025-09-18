// ghostoverlay/main.js
const { app, BrowserWindow, screen, globalShortcut } = require("electron");

let mainWin;

function createOverlay() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;

  mainWin = new BrowserWindow({
    width: 480,
    height: 280,
    x: width - 500,
    y: height - 320,
    alwaysOnTop: true,
    frame: false,              // без системной рамки
    transparent: true,         // прозрачный фон окна
    backgroundColor: "#00000000",
    resizable: false,
    movable: true,
    skipTaskbar: true,
    webPreferences: {
      nodeIntegration: false,  // безопаснее; window.close работает и без этого
      contextIsolation: true,
      sandbox: true
    }
  });

  mainWin.loadFile("index.html");

  mainWin.on("closed", () => {
    mainWin = null;
    console.log("✅ HUD закрыт");
    app.quit();
  });
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWin) {
      if (mainWin.isMinimized()) mainWin.restore();
      mainWin.focus();
    }
  });

  app.whenReady().then(() => {
    createOverlay();

    // Хоткей: Cmd/Ctrl + Shift + O — показать/спрятать HUD
    globalShortcut.register("CommandOrControl+Shift+O", () => {
      if (!mainWin) return;
      if (mainWin.isVisible()) mainWin.hide();
      else mainWin.show();
    });
  });

  app.on("window-all-closed", () => app.quit());
}
