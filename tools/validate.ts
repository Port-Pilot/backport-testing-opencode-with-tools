import { tool } from "@opencode-ai/plugin"
import { exec } from "child_process"
import { promisify } from "util"
import { resolve, dirname } from "path"
import { fileURLToPath } from "url"
import { writeFileSync } from "fs"
import { tmpdir } from "os"

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
// validate Tool
// ---------------------------------------------------------------------------

export default tool({
  description:
    "Validate a patch by applying it to the target repository. " +
    "For the TensorFlow backport dataset, use 'hunk' mode to run " +
    "git apply --check against the target ref in an isolated worktree. " +
    "'full' mode applies the patch and only runs build/test/PoC commands " +
    "when VALIDATE_BUILD_CMD, VALIDATE_TEST_CMD, or VALIDATE_POC_CMD are set.",
  args: {
    ref: tool.schema
      .string()
      .describe("Git commit hash or ref to apply the patch against."),
    patch: tool.schema
      .string()
      .describe("The patch string to validate (diff format)."),
    mode: tool.schema
      .enum(["hunk", "full"])
      .describe(
        "Use 'hunk' to test applying a single hunk. " +
        "Use 'full' to test the complete patch plus optional environment-configured checks."
      ),
    err_msg: tool.schema
      .string()
      .optional()
      .describe(
        "Expected error message from PoC (used to verify if patch fixed the bug in 'full' mode)."
      ),
    revise_context: tool.schema
      .boolean()
      .optional()
      .describe(
        "Force revise_context on patch apply (e.g. if the patch failed with a context mismatch previously)."
      ),
  },
  async execute(args) {
    const scriptPath = getScriptPath("validate.py")
    const repoDir = getRepoDir()
    const pythonExe = getPythonExe()


    // Write the patch to a temporary file to avoid CLI argument length limits
    const patchFile = resolve(tmpdir(), `portgpt_patch_${Date.now()}.diff`)
    try {
      writeFileSync(patchFile, args.patch, "utf-8")
    } catch (e: any) {
      return `Error writing patch to temp file: ${e.message}`
    }

    let cmd = `${pythonExe} "${scriptPath}" --repo "${repoDir}" --ref "${args.ref}" --patch_file "${patchFile}" --mode "${args.mode}"`
    if (args.err_msg) {
      const escapedErrMsg = args.err_msg.replace(/"/g, '\\"')
      cmd += ` --err_msg "${escapedErrMsg}"`
    }
    if (args.revise_context) {
      cmd += " --revise_context"
    }

    try {
      const { stdout, stderr } = await execAsync(cmd, {
        maxBuffer: 10 * 1024 * 1024,
        timeout: 90 * 60_000, // 90 minutes max (compilation can take very long)
      })
      if (stderr) {
        console.error(`[validate] stderr: ${stderr}`)
      }
      return stdout.trim()
    } catch (error: any) {
      const msg = error.stderr || error.message || String(error)
      return `Error executing validate: ${msg}`
    }
  },
})
