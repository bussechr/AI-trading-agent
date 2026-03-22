import fs from "node:fs"
import path from "node:path"
import process from "node:process"

const root = process.cwd()
const packageJson = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"))
const pnpmDir = path.join(root, "node_modules", ".pnpm")

function fail(message) {
  console.error(`[dashboard-doctor] ERROR: ${message}`)
  process.exitCode = 2
}

function ensureExists(relativePath, description) {
  const fullPath = path.join(root, relativePath)
  if (!fs.existsSync(fullPath)) {
    fail(`${description} missing at ${relativePath}`)
  }
}

ensureExists("node_modules", "node_modules")
ensureExists("pnpm-lock.yaml", "pnpm lockfile")

const driftingSpecs = []
for (const sectionName of ["dependencies", "devDependencies"]) {
  const section = packageJson[sectionName] || {}
  for (const [name, version] of Object.entries(section)) {
    if (String(version).trim() === "latest") {
      driftingSpecs.push(`${sectionName}:${name}`)
    }
  }
}

if (driftingSpecs.length > 0) {
  fail(`unpinned dependencies remain:\n${driftingSpecs.join("\n")}`)
}

const nextSpec = String(packageJson.dependencies?.next || "").trim()
const nextDirs = fs.existsSync(pnpmDir)
  ? fs.readdirSync(pnpmDir).filter((entry) => entry.startsWith(`next@${nextSpec}`))
  : []

if (nextDirs.length === 0) {
  fail(`no Next virtual-store entries found for ${nextSpec}`)
}

const nextSymlinkPath = path.join(root, "node_modules", "next")
if (!fs.existsSync(nextSymlinkPath)) {
  fail(`active Next symlink missing at ${path.relative(root, nextSymlinkPath)}`)
}

const activeNextPath = fs.realpathSync(nextSymlinkPath)
const nextModuleRoot = path.dirname(activeNextPath)
const swcHelpersCandidates = [
  path.join(root, "node_modules", "@swc", "helpers", "package.json"),
  ...(fs.existsSync(pnpmDir)
    ? fs
        .readdirSync(pnpmDir)
        .filter((entry) => entry.startsWith("@swc+helpers@"))
        .map((entry) => path.join(pnpmDir, entry, "node_modules", "@swc", "helpers", "package.json"))
    : []),
]
const swcHelpersPath = swcHelpersCandidates.find((candidate) => fs.existsSync(candidate))
const nextAppLoaderPathCandidates = [
  path.join(nextModuleRoot, "next", "dist", "build", "webpack", "loaders", "next-app-loader.js"),
  path.join(nextModuleRoot, "next", "dist", "build", "webpack", "loaders", "next-app-loader", "index.js"),
]
const nextAppLoaderPath = nextAppLoaderPathCandidates.find((candidate) => fs.existsSync(candidate))
const nextFlightLoaderPath = path.join(
  nextModuleRoot,
  "next",
  "dist",
  "build",
  "webpack",
  "loaders",
  "next-flight-client-entry-loader.js",
)
const lightningCssPattern = /^lightningcss-(linux|darwin|win32)-/
const lightningDirs = fs.existsSync(pnpmDir) ? fs.readdirSync(pnpmDir).filter((entry) => lightningCssPattern.test(entry)) : []

if (!swcHelpersPath) {
  fail("@swc/helpers is missing from node_modules and pnpm virtual store")
}
if (!nextAppLoaderPath) {
  fail("next-app-loader missing from active Next module path")
}
if (!fs.existsSync(nextFlightLoaderPath)) {
  fail(`next-flight-client-entry-loader missing from active Next module path: ${path.relative(root, nextFlightLoaderPath)}`)
}
if (lightningDirs.length === 0) {
  fail("no platform lightningcss package found under node_modules/.pnpm")
}

if (process.exitCode && process.exitCode !== 0) {
  process.exit(process.exitCode)
}

console.log("[dashboard-doctor] OK")
console.log(`[dashboard-doctor] next=${nextSpec}`)
console.log(`[dashboard-doctor] next-store-candidates=${nextDirs.length}`)
console.log(`[dashboard-doctor] next-active=${path.relative(root, activeNextPath)}`)
console.log(`[dashboard-doctor] lightningcss-platforms=${lightningDirs.join(",")}`)
