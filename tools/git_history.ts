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

function getPythonExe(): string {
  const defaultPython = process.platform === "win32" ? "python" : "python3"
  return process.env.PORTGPT_PYTHON || defaultPython
}

// ---------------------------------------------------------------------------
// git_history Tool
// ---------------------------------------------------------------------------

export default tool({
  description:
    "Get change history for a specific code region (hunk) in the target repository. " +
    "This helps analyze whether the code logic already existed before a commit " +
    "or was added/moved, so you can track where patch logic should be ported to.",
  args: {
    filepath: tool.schema
      .string()
      .describe("Path of the file to trace (e.g. 'net/socket.c')."),
    start_line: tool.schema
      .number()
      .describe("Start line number of the code region."),
    end_line: tool.schema
      .number()
      .describe("End line number of the code region."),
    start_commit: tool.schema
      .string()
      .describe("Start commit hash for the history search (e.g. merge base)."),
    end_commit: tool.schema
      .string()
      .describe("End commit hash for the history search (e.g. new patch parent)."),
  },
  async execute(args) {
    const scriptPath = getScriptPath("git_history.py")
    const repoDir = getRepoDir()
    const pythonExe = getPythonExe()


    const cmd = [
      pythonExe,
      `"${scriptPath}"`,
      "--repo", `"${repoDir}"`,
      "--filepath", `"${args.filepath}"`,
      "--start_line", String(args.start_line),
      "--end_line", String(args.end_line),
      "--start_commit", `"${args.start_commit}"`,
      "--end_commit", `"${args.end_commit}"`,
    ].join(" ")

    try {
      const { stdout, stderr } = await execAsync(cmd, {
        maxBuffer: 5 * 1024 * 1024,
        timeout: 60_000,
      })
      if (stderr) {
        console.error(`[git_history] stderr: ${stderr}`)
      }
      return stdout.trim()
    } catch (error: any) {
      const msg = error.stderr || error.message || String(error)
      return `Error executing git_history: ${msg}`
    }
  },
})
