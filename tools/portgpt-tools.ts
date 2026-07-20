import git_history from "./git_history"
import git_show from "./git_show"
import locate_symbol from "./locate_symbol"
import validate from "./validate"
import viewcode from "./viewcode"

export default async () => {
  return {
    tool: {
      git_history,
      git_show,
      locate_symbol,
      validate,
      viewcode,
    },
  }
}