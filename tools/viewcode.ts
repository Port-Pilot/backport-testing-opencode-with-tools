import { tool } from "@opencode-ai/plugin"
import { exec } from "child_process"
import { promisify } from "util"
import { resolve, dirname } from "path"
import { fileURLToPath } from "url"

const execAsync = promisify(exec)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Path to a Python script in the scripts/ subdirectory next to this file.
 */
function getScriptPath(scriptName: string): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), "scripts", scriptName)
}

/**
 * Get the git repository path.
 * Uses PORTGPT_REPO_DIR env var if set, otherwise falls back to the
 * current working directory (i.e. the repo opencode was opened in).
 */
function getRepoDir(): string {
  return process.env.PORTGPT_REPO_DIR || process.cwd()
}

/**
 * Determine which Python executable to use.
 */
function getPythonExe(): string {
  const defaultPython = process.platform === "win32" ? "python" : "python3"
  return process.env.PORTGPT_PYTHON || defaultPython
}

// ---------------------------------------------------------------------------
// viewcode Tool
// ---------------------------------------------------------------------------

export default tool({
  description:
    "View source code from a specific git ref of the target repository. " +
    "Shows lines between startline and endline (inclusive, 1-indexed). " +
    "Use this to inspect code context around a patch location in either " +
    "the new or old version of the project.",
  args: {
    ref: tool.schema
      .string()
      .describe("Git commit hash or ref to view the file from."),
    path: tool.schema
      .string()
      .describe(
        "Relative path of the file from the project root " +
        "(e.g. 'net/socket.c' or 'lib/utils.h')."
      ),
    startline: tool.schema
      .number()
      .describe("First line to display (1-indexed)."),
    endline: tool.schema
      .number()
      .describe("Last line to display (1-indexed)."),
  },
  async execute(args) {
    const scriptPath = getScriptPath("viewcode.py")
    const repoDir = getRepoDir()
    const pythonExe = getPythonExe()


    const cmd = [
      pythonExe,
      `"${scriptPath}"`,
      "--repo", `"${repoDir}"`,
      "--ref", `"${args.ref}"`,
      "--path", `"${args.path}"`,
      "--startline", String(args.startline),
      "--endline", String(args.endline),
    ].join(" ")

    try {
      const { stdout, stderr } = await execAsync(cmd, {
        maxBuffer: 10 * 1024 * 1024, // 10 MB — source code can be large
        timeout: 60_000,              // 1 minute timeout
      })
      if (stderr) {
        console.error(`[viewcode] stderr: ${stderr}`)
      }
      return stdout.trim()
    } catch (error: any) {
      const msg = error.stderr || error.message || String(error)
      return `Error executing viewcode: ${msg}`
    }
  },
})
