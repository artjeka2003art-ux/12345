// ghostoverlay/start.js
const { spawn } = require("child_process");
const path = require("path");
const electron = require("electron");

const appPath = __dirname; // путь к ghostoverlay (там лежит main.js и index.html)

const child = spawn(electron, [appPath], {
  stdio: "inherit"
});

child.on("close", code => process.exit(code));

