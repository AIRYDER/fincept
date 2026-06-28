/**
 * Standalone test runner for quant-foundry/promotion page tests.
 * Uses tsx with the test tsconfig (which redirects next/navigation
 * and next/link to test mocks via tsconfig paths).
 */
const { execSync } = require("child_process");
const path = require("path");

const testFile = path.join(
  __dirname,
  "..",
  "src",
  "app",
  "quant-foundry",
  "promotion",
  "page.test.tsx",
);

try {
  execSync(`npx --yes tsx --tsconfig tsconfig.test.json "${testFile}"`, {
    cwd: path.join(__dirname, ".."),
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}
