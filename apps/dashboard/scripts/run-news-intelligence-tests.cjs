/**
 * Standalone test runner for news-intelligence.test.ts
 * Uses tsx for TypeScript execution without Jest/Vitest.
 */
const { execSync } = require("child_process");
const path = require("path");

const testFile = path.join(
  __dirname,
  "..",
  "src",
  "components",
  "news",
  "news-intelligence.test.ts",
);

try {
  execSync(`npx --yes tsx "${testFile}"`, {
    cwd: path.join(__dirname, ".."),
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}
