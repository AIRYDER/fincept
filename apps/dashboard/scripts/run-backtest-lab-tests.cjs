/**
 * Standalone test runner for backtest-lab.test.ts
 * Uses tsx for TypeScript execution without Jest/Vitest.
 */
const { execSync } = require("child_process");
const path = require("path");

const testFile = path.join(
  __dirname,
  "..",
  "src",
  "components",
  "backtest",
  "backtest-lab.test.ts",
);

try {
  execSync(`npx --yes tsx "${testFile}"`, {
    cwd: path.join(__dirname, ".."),
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}
