/**
 * Standalone test runner for mock-badge.test.tsx.
 * Uses tsx with a test tsconfig that enables the automatic JSX runtime
 * so components that rely on the Next.js JSX runtime can be rendered
 * outside of Next.js.
 */
const { execSync } = require("child_process");
const path = require("path");

const testFile = path.join(
  __dirname,
  "..",
  "src",
  "components",
  "widgets",
  "mock-badge.test.tsx",
);

try {
  execSync(`npx --yes tsx --tsconfig tsconfig.test.json "${testFile}"`, {
    cwd: path.join(__dirname, ".."),
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}
