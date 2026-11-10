# Solvewright connector

Practise live technical interviews in [Solvewright Interview Lab](https://solvewright.app/coding/interviews)
with the Claude Code or Codex you already use. The interviewer runs on your computer, under your own
sign-in and plan. Solvewright provides the room: the problem, the editor, the tests, the conversation,
your history and your skill profile.

```sh
uvx solvewright
```

No uv? `pipx run solvewright` works the same way, or `python3 -m pip install --user solvewright` and
then `solvewright`. Python 3.10 or newer. The package has no dependencies.

## What happens when you run it

1. The terminal lists this computer, the agents it found and where interviews are kept.
2. It prints a short code and opens `solvewright.app/connect`. Check that the page shows the same
   code, then press **Connect this computer**. You must be signed in to Solvewright.
3. The terminal says which account it joined. Keep the window open while you practise; it prints one
   line for each thing it does (a round prepared, a reply, tests recorded).
4. Press Ctrl+C to stop. Nothing keeps running in the background, and nothing starts again by itself.

Next time, `solvewright` reconnects without a new code. `solvewright disconnect` removes this
computer from your account; you can also remove it from Interview Lab, which stops its key at once.

| Command | Does |
| --- | --- |
| `solvewright` | Connect if needed, then wait for rounds |
| `solvewright connect` | Approve this computer again, with a new key |
| `solvewright status` | Show the connection and the agents found here |
| `solvewright disconnect` | Remove this computer from your account |
| `solvewright doctor` | Check Python, Claude Code, Codex and the sandbox |

## What it can and cannot do

The connector is deliberately small. Every request it accepts is one of a fixed list: report its
status, start a round, pass your message to the interviewer, save your files, record a test result,
stop the interviewer, and sync your history. There is no request that runs a command.

- **It opens no port.** It connects out to Solvewright over HTTPS and asks for work.
- **It only takes requests from your account.** This computer's key is issued after you approve it
  in the browser, is stored only here (`connection.json`, readable by you alone), and the service
  keeps a hash of it.
- **It never sees your Claude or ChatGPT credentials.** It starts the official `claude` or `codex`
  program, unmodified, which signs in the way it always does. Usage counts against your own plan.
- **It never runs your code.** Public tests run in your browser, in Pyodide (Python compiled to
  WebAssembly). The connector only stores the files you tested and the result.
- **It does not update itself.** What runs is the version you installed from PyPI. The interviewer's
  instructions ship inside the package (`src/solvewright/interviewer/`), so you can read exactly what
  your agent is told.

### The interviewer's sandbox

The interviewer writes the problem and checks its own reference solution, so it needs to run code.
It does that inside its own agent's sandbox, confined to the folder of the current round:

| | Commands | Files | Network |
| --- | --- | --- | --- |
| Claude Code, macOS and Linux | Claude Code's OS sandbox | Read and write only in the round folder; the rest of your home folder is unreadable | None |
| Codex, macOS and Linux | Codex's sandbox with a Solvewright permission profile | Read and write in your interview folders; the rest of your home folder is unreadable | None |
| Claude Code, native Windows | No command tool at all: its sandbox needs macOS, Linux or WSL2 | The file tools stay in the round folder | None |

Claude Code runs with `--restricted --safe-mode --strict-mcp-config`: your own CLAUDE.md, hooks,
plugins and MCP servers are not loaded. Codex runs through its documented app-server with its plugins,
apps, hooks, web search, memories and every MCP server you configured switched off for the interview.
If a platform cannot provide one of these limits, the connector removes the capability rather than the
limit, and says so in the terminal and in Interview Lab. On Linux, Claude Code's sandbox needs
`bubblewrap` and `socat`.

## What is stored where

On this computer, in one folder (`solvewright status` prints it):

| System | Folder |
| --- | --- |
| macOS | `~/Library/Application Support/Solvewright` |
| Windows | `%LOCALAPPDATA%\Solvewright` |
| Linux | `$XDG_DATA_HOME/solvewright`, by default `~/.local/share/solvewright` |

Each round is a folder holding the candidate files, the interviewer's private files, snapshots of
what you submitted, and the conversation. When you are signed in, Interview Lab also keeps a copy in
your Solvewright account so you can continue on another computer; deleting your account deletes it.
Your agent sends what it reads to its own provider (Anthropic or OpenAI) under your account's terms,
as it does in any other session. Details: [solvewright.app/privacy](https://solvewright.app/privacy#privacy-interviews).

## Read the code

About 1,800 lines of Python in `src/solvewright/`:

| File | Role |
| --- | --- |
| `cli.py` | The commands and everything the terminal prints |
| `connection.py` | The browser approval and the saved key |
| `service.py` | The only network calls: HTTPS to Solvewright's account service |
| `worker.py` | The fixed list of requests and the loop that fetches them |
| `agents.py` | How Claude Code and Codex are started, and their sandbox settings |
| `runtime.py` | Rounds on disk, account-history sync, the interviewer's turns |
| `interviewer/` | The instructions and references the interviewer reads |

The account service address and its publishable key are in `settings.py`. They are the same public
values every visitor's browser receives from solvewright.app.

Run the tests from this folder with `PYTHONPATH=src python -m unittest discover -s tests` (on Windows, set `PYTHONPATH=src` first).

## Licence

MIT. Solvewright is not affiliated with Anthropic or OpenAI. Claude Code and Codex are their
respective owners' products; this program runs the versions you installed.
