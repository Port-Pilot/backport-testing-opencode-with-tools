Run only the first row:
python .\run_codex_backports.py `
  --repo .\tensorflow `
  --csv .\tenser-flow.csv `
  --output .\tenser-flow-results.csv `
  --limit 1

Run row 5 only:
python .\run_codex_backports.py `
  --repo .\tensorflow `
  --csv .\tenser-flow.csv `
  --output .\tenser-flow-results.csv `
  --start 5 `
  --limit 1

Run rows 10 to 19:
python .\run_codex_backports.py `
  --repo .\tensorflow `
  --csv .\tenser-flow.csv `
  --output .\tenser-flow-results.csv `
  --start 10 `
  --limit 10
  
Force rerun even if result already exists:
python .\run_codex_backports.py `
  --repo .\tensorflow `
  --csv .\tenser-flow.csv `
  --output .\tenser-flow-results.csv `
  --limit 1 `
  --rerun-completed

Use a specific OpenCode model:
python .\run_codex_backports.py `
  --repo .\tensorflow `
  --csv .\tenser-flow.csv `
  --output .\tenser-flow-results.csv `
  --model openai/gpt-5.5 `
  --limit 1

Use a specific OpenCode agent:
python .\run_codex_backports.py `
  --repo .\tensorflow `
  --csv .\tenser-flow.csv `
  --output .\tenser-flow-results.csv `
  --agent build `
  --limit 1

Use model variant/reasoning effort:
python .\run_codex_backports.py `
  --repo .\tensorflow `
  --csv .\tenser-flow.csv `
  --output .\tenser-flow-results.csv `
  --model openai/gpt-5.5 `
  --variant medium `
  --start 1 `
  --limit 1

Show all available options:
python .\run_codex_backports.py --help

Clean the stale worktree folder if you get the “not a working tree” error again:
Remove-Item -LiteralPath ".\.opencode-backport-worktrees\row-0001-fd8e10fb9017" -Recurse -Force