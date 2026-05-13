const { execSync } = require("child_process");
const path = require("path");

const testFile = path.join(
  __dirname,
  "..",
  "src",
  "components",
  "overview",
  "operator-briefing.test.ts",
);

try {
  execSync(`npx tsx "${testFile}"`, {
    cwd: path.join(__dirname, ".."),
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}
