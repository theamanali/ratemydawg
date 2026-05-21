#!/usr/bin/env node
const fs = require("fs")
const path = require("path")
const { execSync } = require("child_process")

const pkgPath = path.resolve(__dirname, "../package.json")
const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"))
const original = JSON.stringify(pkg, null, 2)

pkg.manifest.host_permissions = pkg.manifest.host_permissions.filter(
  (h) => !h.includes("railway") && !h.includes("localhost")
)

fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2))

try {
  execSync("plasmo build", { stdio: "inherit" })
} finally {
  fs.writeFileSync(pkgPath, original)
}
