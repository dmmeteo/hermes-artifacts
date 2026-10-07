// Builds the shared panel once and the Hermes host adapter. The panel's bytes are copied unchanged into
// the service (app/console) and the plugin (plugin/dashboard/dist/console); verify gates their equality.
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync } from "node:fs";
import { build } from "esbuild";

execFileSync("npx", ["tsc", "--noEmit"], { stdio: "inherit" });
const common = { bundle: true, format: "iife", target: "es2020", minify: true, legalComments: "none", logLevel: "warning" };
await build({ ...common, entryPoints: ["src/panel/main.tsx"], outfile: "../app/console/console.js",
  jsx: "transform", jsxFactory: "React.createElement", jsxFragment: "React.Fragment",
  define: { "process.env.NODE_ENV": '"production"' } });
await build({ ...common, entryPoints: ["src/host/hermes.tsx"], outfile: "../plugin/dashboard/dist/index.js",
  jsx: "transform", jsxFactory: "React.createElement", jsxFragment: "React.Fragment" });
copyFileSync("src/panel/style.css", "../app/console/console.css");
copyFileSync("src/panel/console.html", "../app/console/console.html");
copyFileSync("src/host/style.css", "../plugin/dashboard/dist/style.css");
mkdirSync("../plugin/dashboard/dist/console/assets", { recursive: true });
copyFileSync("../app/console/console.html", "../plugin/dashboard/dist/console/console.html");
copyFileSync("../app/console/console.js", "../plugin/dashboard/dist/console/assets/console.js");
copyFileSync("../app/console/console.css", "../plugin/dashboard/dist/console/assets/console.css");
