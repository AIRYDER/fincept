/**
 * Standalone test runner for shadow-news-impact-panel.test.tsx.
 * Uses tsx for TypeScript/TSX execution without Jest/Vitest.
 */
const { execSync } = require("child_process");
const path = require("path");

const testFile = path.join(
  __dirname,
  "..",
  "src",
  "components",
  "news-impact",
  "shadow-news-impact-panel.test.tsx",
);

try {
  execSync(`npx tsx "${testFile}"`, {
    cwd: path.join(__dirname, ".."),
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}
