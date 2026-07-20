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
// locate_symbol Tool
// ---------------------------------------------------------------------------

export default tool({
  description:
    "Locate a symbol (function name, variable, struct, etc.) in a specific " +
    "git ref of the target repository. Uses ctags to build a symbol table. " +
    "If the exact symbol is not found, returns the closest match by " +
    "Levenshtein distance. Returns results in 'file_path:line_number' format.",
  args: {
    ref: tool.schema
      .string()
      .describe("Git commit hash or ref to search in."),
    symbol: tool.schema
      .string()
      .describe(
        "The symbol name to locate (e.g. a function name like " +
        "'ksmbd_conn_handler' or a struct name)."
      ),
  },
  async execute(args) {
    const scriptPath = getScriptPath("locate_symbol.py")
    const repoDir = getRepoDir()
    const pythonExe = getPythonExe()


    const cmd = [
      pythonExe,
      `"${scriptPath}"`,
      "--repo", `"${repoDir}"`,
      "--ref", `"${args.ref}"`,
      "--symbol", `"${args.symbol}"`,
    ].join(" ")

    try {
      const { stdout, stderr } = await execAsync(cmd, {
        maxBuffer: 10 * 1024 * 1024,
        timeout: 120_000, // 2 minutes (ctags might take a while on large repos)
      })
      if (stderr) {
        console.error(`[locate_symbol] stderr: ${stderr}`)
      }
      return stdout.trim()
    } catch (error: any) {
      const msg = error.stderr || error.message || String(error)
      return `Error executing locate_symbol: ${msg}`
    }
  },
})
