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
// git_show Tool
// ---------------------------------------------------------------------------

export default tool({
  description:
    "Show commit message, stats, and context for a specific git ref. " +
    "Can optionally search the commit for a specific code context to find " +
    "where old code was moved or modified in this commit.",
  args: {
    ref: tool.schema
      .string()
      .describe("Git commit hash or ref to show."),
    context: tool.schema
      .string()
      .optional()
      .describe(
        "Optional code block (string) to search for in the commit's patch. " +
        "If provided, the tool will try to find the most similar block " +
        "in the commit and report its new file path and line numbers."
      ),
  },
  async execute(args) {
    const scriptPath = getScriptPath("git_show.py")
    const repoDir = getRepoDir()
    const pythonExe = getPythonExe()


    // Build command, taking care of multiline string context
    let cmd = `${pythonExe} "${scriptPath}" --repo "${repoDir}" --ref "${args.ref}"`
    if (args.context) {
      // Escape newlines and quotes so it can be passed via CLI
      const escapedContext = args.context
        .replace(/\\/g, "\\\\")
        .replace(/\n/g, "\\n")
        .replace(/"/g, '\\"')
      cmd += ` --context "${escapedContext}"`
    }

    try {
      const { stdout, stderr } = await execAsync(cmd, {
        maxBuffer: 5 * 1024 * 1024,
        timeout: 60_000,
      })
      if (stderr) {
        console.error(`[git_show] stderr: ${stderr}`)
      }
      return stdout.trim()
    } catch (error: any) {
      const msg = error.stderr || error.message || String(error)
      return `Error executing git_show: ${msg}`
    }
  },
})
