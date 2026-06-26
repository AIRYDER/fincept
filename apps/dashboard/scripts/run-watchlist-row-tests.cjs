/**
 * Standalone test runner for watchlist-row.test.tsx.
 * Uses tsx for TypeScript/TSX execution without Jest/Vitest.
 */
const { execSync } = require("child_process");
const path = require("path");

const testFile = path.join(
  __dirname,
  "..",
  "src",
  "components",
  "widgets",
  "watchlist-row.test.tsx",
);

try {
  execSync(`npx tsx --tsconfig tsconfig.test.json "${testFile}"`, {
    cwd: path.join(__dirname, ".."),
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}
