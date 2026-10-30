# Pry

A local terminal assistant with interactive inspectors and Ollama-powered AI.

Run `python3 ai-tool.py` in a terminal to open the TUI. Requires Python 3.10+,
macOS or Linux, and **Ollama running locally with a downloaded model**. Ollama
provides the model runtime; installing this Python app alone is not sufficient.

## Install and start Ollama

### macOS

Download Ollama from [ollama.com/download](https://ollama.com/download), install
the application, and open it. Follow the prompts to make the `ollama` command
available in your terminal. Keep Ollama running while using this assistant.

### Linux

Install using the [official installer](https://ollama.com/download/linux):

```sh
curl -fsSL https://ollama.com/install.sh | sh
```

On systemd installations, check/start the service with:

```sh
sudo systemctl enable --now ollama
systemctl status ollama
```

If you are not using a service, run `ollama serve` in a separate terminal and
leave it running. This also works for a manual macOS installation. Only start
one server; an address-already-in-use message usually means it is running already.

### Download a model and launch the assistant

From this project directory:

```sh
ollama --version
ollama pull qwen2.5-coder:7b
./build-model.sh linux-cmds-7b
ollama list
python3 ai-tool.py --doctor
python3 ai-tool.py
```

The explicit `linux-cmds-7b` build name matches the app's initial default.
`./build-model.sh` without an argument instead creates `ops-gpt-7b`; select that
name with `/model ops-gpt-7b` if you use it. The bundled `Modelfile` builds on
`qwen2.5-coder:7b` and adds terminal-focused instructions and generation settings.

The app connects to `http://localhost:11434`; no API key or cloud account is
needed for downloaded local models. Internet access is needed for installation
and model downloads; inference can then run offline. Use local model tags below
rather than `:cloud` tags to keep inference on your machine.

## Recommended local models

Start with **`qwen2.5-coder:7b`**, the base used by this project's bundled model.
The following are practical starting points for terminal commands and shell
scripting, rather than a ranking of every available model:

| Model | Approx. download | Suggested machine memory | When to use |
| --- | --- | --- | --- |
| `qwen2.5-coder:1.5b` | 1 GB | 8 GB | Lightweight option for simple commands; less capable on complex troubleshooting. |
| `qwen2.5-coder:3b` | 1.9 GB | 8–16 GB | Faster, smaller alternative when 7B is too slow. |
| `qwen2.5-coder:7b` | 4.7 GB | 16 GB+ | Recommended balance for this app. |
| `qwen2.5-coder:14b` | 9 GB | 24–32 GB+ | More capacity for complex scripting and explanations, with higher latency. |
| `qwen2.5-coder:32b` | 20 GB | 48–64 GB+ | Larger workstations with ample memory; substantial compute requirements. |

Download sizes are approximate for the default tags listed in the
[Ollama model library](https://ollama.com/library/qwen2.5-coder). Memory figures
are conservative starting suggestions, not guaranteed minimums: runtime memory
also includes context and other applications. Apple Silicon uses unified memory;
on Linux, available GPU VRAM affects acceleration and CPU offloading. CPU-only
inference works but may be slow. This app currently requests an 8,192-token context.

To try a smaller model without rebuilding the bundled variant:

```sh
ollama pull qwen2.5-coder:3b
python3 ai-tool.py --model qwen2.5-coder:3b "Explain find -type f"
```

In the TUI, use `/models` or `/model qwen2.5-coder:3b` to select and save the
model choice. To retain the bundled terminal-focused instructions with another
base, change the `FROM` line in `Modelfile`, build under a new name (for example,
`./build-model.sh terminal-coder-3b`), and select that name with `/model`.

### Setup troubleshooting

- **`ollama: command not found`:** finish the Ollama installation and reopen your
  terminal so its PATH is refreshed.
- **Connection refused:** open the macOS Ollama app, start the Linux service, or
  run `ollama serve`; then retry `python3 ai-tool.py --doctor`.
- **Model not found:** check `ollama list`, pull the desired base model, and either
  build `linux-cmds-7b` as above or select an installed model with `/model`.
- **Slow responses or out of memory:** try the 3B or 1.5B model, close other
  memory-heavy applications, and inspect loaded models with `ollama ps`.

## CLI examples

```sh
python3 ai-tool.py --doctor
python3 ai-tool.py "How do I find large files?"
git status --short | python3 ai-tool.py "Explain this output"
python3 ai-tool.py --model linux-cmds-7b "Explain rsync --archive"
```

CLI responses go to stdout; failures go to stderr with a nonzero exit code.
Piped input is limited to 128 KiB and included as reference data. Both CLI and TUI
use the model's system prompt, local machine information, installed documentation
and context budgeting. Model responses never execute commands automatically.

## Commands and keys

Follow-up questions use the preceding command output or AI response as context.
Local results such as `/docs rsync`, `/recipe show NAME`, and `/doctor` also
become conversational context. Questions submitted while documentation is loading
wait for that result, so `what does --archive do?` can refer to the manual you
just opened. These local results remain available when a saved session is reopened.
The **Following** indicator above the transcript shows the current conversational
focus. `/topic` starts a fresh topic while keeping the transcript and pinned facts;
it clears attached files and the browsed location so they do not carry into the new
subject. `/topic YOUR QUESTION` starts fresh and asks the question immediately.
Topic boundaries survive reopening a session.

After `/docs COMMAND`, follow-up questions search the cached installed manual for
relevant flags and terms, including sections beyond the displayed initial excerpt.
Matches include numbered lines and references such as `[rsync:L120-L137]`. The model
is asked to quote supporting text and cite those references; supplied reference IDs
are also shown below its answer. This provides inspectable sources, not a guarantee
that every generated claim is correct. Search covers up to 1 MiB per manual and
returns up to three matching windows; the prompt budget can further shorten them.
Use `/docs rsync --archive` to search directly or `/docs rsync L120-L137` to
reopen an exact cited range (up to 100 lines).
For example, after `/cmd ls -al`, ask `largest file?`; after an explanation, ask
`why?` or `what does that flag do?`. Explicit new topics take precedence. Long
recent outputs retain beginning/end excerpts; answers based on incomplete data
should say so. Context refers to captured observations, not an automatic refresh
of your filesystem or a command that runs without your action.

`/help` lists commands; `/keys` shows shortcuts. Ctrl+P/N recalls input, Ctrl+R
retries the last AI question, Ctrl+C stops active work (otherwise exits).
Alt+Enter inserts a newline. Tab completes slash commands, otherwise Tab/Shift+Tab
moves inspector focus. **Ctrl+K** opens the searchable workspace switcher from any
screen; type a workspace name, use arrows to select, and Enter to open it.

### Terminal-native interface

The interface uses a consistent charcoal palette, warm accents, cool context
labels, and highlighted selections. Turn headings distinguish questions, assistant
answers, terminal output, and local references. A separate composer shows whether
you are asking AI, capturing a command, or preparing a terminal handoff.

The Pry header stays visible across every workspace, showing the current screen,
activity, model, and working directory as space permits. **Panels** or **Ctrl+K**
opens a filterable workspace switcher with numbered shortcuts. Workspace tabs and
actions use compact horizontal strips; overflow arrows reveal additional choices,
and Left/Right moves between actions. Forms support clickable fields and reverse
navigation with Shift+Tab. Inspectors keep secondary actions under **More...**.

Assistant responses render Markdown headings, bold/italic text, inline code,
links, lists, and quotes directly in the terminal, including while streaming.
Prose wraps at word boundaries with hanging indents for lists. Tables use aligned
columns when they fit and switch to labelled fields in narrow panes. Fenced code
keeps its existing command cards and actions once the response is complete.
Copy Response preserves the original Markdown; terminal output stays literal.

Use **Ctrl+B** or `/sidebar` to toggle the saved
session sidebar. The preference persists across launches, and toggling preserves
your draft. Below 100 columns, Ctrl+B shows sessions as a full-width list instead
of squeezing the conversation into a narrow pane.
Long terminal output and local references initially show 12 lines; **Show all**
expands them and **Collapse output** folds them again. Copy Response always copies
the complete captured response. When scrolled up, the status bar shows how to
return to following new output.

Each saved session has an **x** delete control. Click it, then **Confirm?** on that
same row to delete the session. Opening a session cancels the confirmation.
Deleting a session does not remove its captured command log files.

- `/cmd COMMAND`: stream stdout/stderr with a bounded 48 KiB tail in the UI.
- `/run COMMAND`: hand the terminal to an interactive program, such as `top`,
  `ssh`, an editor or `sudo`. Exit the program and press Enter to return.
- `/stop`: cancel active work and queued turns. Cancelling model work discards
  stale output; a blocked HTTP read can remain until its timeout.
- `/cd DIRECTORY`, `/shell /bin/zsh`, `/timeout 120`: set cwd, shell and command
  timeout. Timeout `0` disables it; interactive handoff has no timeout.
- `/docs rsync`: view installed manual/help with source attribution.
- `/doctor`: inspect OS, shell, installed tools, Ollama and model availability.
- `/rename NAME`, `/search TEXT`: name sessions and filter the saved-session list.
  `/search` without text clears the filter.
- `/pin FACT`, `/pins`, `/unpin NUMBER`, `/unpin all`: maintain session facts.
- `/files`: attach local text files. Prompt context is budget-limited with a
  visible estimate, older-history excerpts, recent turns and truncated references.

Every command gets a fresh shell: **cwd persists; variables, functions and aliases
do not**. Shell startup behavior follows `SHELL -c`; interactive commands inherit
the terminal but the wrapper shell is not a login shell. An EXIT trap records cwd;
commands using `exec` or replacing that trap cannot reliably report changed cwd.
Cancellation targets the captured command process group, including child processes.

## Command cards and troubleshooting

### Interactive inspectors and AI questions

Recognized command output opens an inline inspector. Use Tab / Shift+Tab to
focus its controls, arrows to select entries, Enter to inspect, Left/Back to
return, and Esc to return to the question editor. Clickable controls provide
Refresh, Filter, Copy, Explain, and Add observations to context. Filter matches
displayed text (including log severity); type a filter and press Enter to apply
or Esc to clear. Explain uses the selected observation or current page.

The AI automatically receives the **active inspector's state** with each new
question: selected entry, current detail location, navigation trail, filter,
pause/loading state, and bounded displayed observations. Selecting a file adds
its current metadata and up to 4096 bytes of text preview (regular, non-symlink
files only). For example: run `/cmd ls -al`, select a file, press Esc, and ask
`what is this?` or `what is this file used for?`. The header identifies the
inspector being used. A persistent **Ask AI about:** line immediately above the
prompt identifies its transcript turn number, current page, and selected item.
**Change** cycles through inspectors in the current topic and reveals the chosen
one; **Clear** detaches it. These controls preserve your draft. Each inspector
also offers **Ask AI about this** and marks the active attachment with **Attached
to AI prompt**. Press Esc after navigating to return to typing; the attachment
remains active. `/inspect` displays its context; `/inspect off` detaches it.
Text file previews show line numbers, file type, size, modification time and subtle
syntax colours for common source/config formats. Wrapped lines keep a blank
number gutter; filtering retains original line numbers, and search previews mark
the matching line. File text stays literal, and copy/AI context remain unstyled.
Hover highlights controls and actionable entries, not plain file-content rows or
the inspector's empty background.
Starting another command or `/topic` clears the previous selection. Context is
frozen at question submission, remains with that saved question, and is
budget-limited; binary files and unavailable details are reported explicitly.

| Inspector | Recognized commands | Navigation and actions |
|---|---|---|
| Ports/connections | `lsof -i`, `ss`, `netstat` | Listening connections first; inspect identified processes or captured connection details. |
| Logs | `tail`, `journalctl`, `docker logs`, `podman logs` | Text/severity filter, nearby lines per entry, Explain; streaming commands provide Pause/Resume without stopping capture. `/stop` ends capture. |
| Disk usage | `df`, `du` | Largest `du` entries first; inspect a directory's live KiB totals and browse its files. |
| Containers | `docker ps`, `docker compose ps`, `podman ps` | Inspect status/ports/mounts; commands for logs, resources, shell, start/stop/restart. |
| Services | `systemctl`, `launchctl list` | Live service properties; commands for start/stop/restart and systemd journal logs. launchctl details target the current user's GUI domain. |
| Search | `rg`, `grep`, `find` | Matches grouped by filename; open surrounding source lines, browse the folder, attach the file. Filename/line prefixes are recognized when present. |
| HTTP | `curl` | Captured status/header blocks, body view, nested JSON objects/arrays and value paths. Use `curl -i` to capture headers; timing requires `-w`. Refresh never repeats HTTP requests. |
| Archives | `tar -tf ARCHIVE`, `unzip -l ARCHIVE` | Folder navigation, bounded text previews, selected regular-file extraction command with editable destination. Listings are limited to 10,000 entries. |
| Processes | `ps` with a PID header | CPU/memory sorting when those columns exist, parent/child navigation, live executable/cwd, open files/ports and SIGTERM command. Opt-in 3-second live refresh with Pause. |

Inspector actions that run commands **load the input editor** for review; press
Enter to execute. Interactive container shells use terminal handoff. Replace
`{{destination}}` with a shell-quoted existing destination directory before archive
extraction. Tools and permissions come from your existing machine; missing tools
or inaccessible details appear as unavailable. Captured snapshots and live queries
are labelled. Refresh uses bounded read-only queries where supported; search,
HTTP, and `du` roots reopen captured observations. Use a new command for fresh
search/HTTP results. Complex shell pipelines fall back to ordinary output.

### Code cards

Each fenced block appears once, in its own card. Shell cards show **Edit**, **Run**,
and **More…**. More reveals Copy, Interactive, Explain, Preview, and Save recipe;
**Less** folds these extra actions. Other language blocks offer Copy under More.
Edit loads the command into the input buffer for review. Preview
currently supports simple `rsync` commands by adding `--dry-run --itemize-changes`;
unsupported transformations show an explanation. Terminal turns offer Explain
Output, Next Diagnostic and Verify Result; these ask the model for suggestions.

## Git workspace

Open **Panels > Git** in the header, or enter `/git` (`/git status` and `/git cmd` also
open the panel). It finds the repository containing the conversation's current
working directory, including when you are in a subfolder. Use `/cd PATH` first
if necessary. Closing with Esc returns to your unchanged chat draft.

- **Changes:** select a file and View diff; Stage file includes the whole file
  in the next commit. Conflicted files also appear here for inspection/staging.
- **Ready to commit:** inspect staged diffs, Unstage file to preserve edits but
  leave them out of the commit, or Commit with a message. Only staged files commit.
- **Branches:** compare a local branch with the current branch, switch branches,
  create a new branch, or merge the selected branch into the current branch.
- **Stashes:** save tracked and untracked changes, inspect a saved stash, and
  apply it. Applying retains the stash in the list.
- **Fetch / Pull / Push / Sync:** fetch refreshes remote information; pull uses
  fast-forward only. Sync requires a clean working tree and runs fetch, then
  fast-forward pull if tracking is configured, then push. If branches diverge,
  inspect the output and choose how to merge them. A first push establishes
  tracking using origin, or the sole configured remote.
- **Conflicts:** resolve files using your editor, stage the resolved files, then
  Continue operation. Abort operation is also available for an ongoing merge,
  rebase, cherry-pick or revert; it can discard conflict-resolution edits.
- **Create PR:** enter the target branch and a title. This pushes the current
  branch, then uses authenticated `gh` to create the PR. Without authenticated
  `gh`, it opens GitHub's compare page for completion in your browser. The target
  field starts with `main`; change it if your repository uses another base.

Everything uses your installed Git, identity, hooks, signing configuration, SSH
agent and credential helpers. No credentials are stored by this tool. PR creation
supports `github.com` remotes. Jobs run asynchronously; Ctrl+C cancels the current
Git process, then Refresh shows its resulting state. Commands requiring terminal
input (such as an SSH password or signing prompt) can be run with `/run git …`.
Hooks run normally and their failures appear in the output pane.

Keyboard: **1–4** selects a view; **Tab / Shift+Tab** moves focus between list,
actions and output; arrows select; Enter inspects or activates the focused action.
Mouse clicks select views/files/actions, and the wheel scrolls output. Forms use
Tab to change fields, Enter to apply, and Esc to cancel. Shortcuts include
`s` stage, `u` unstage, `c` commit, `r` refresh, `f` fetch, `p` pull, `P` push,
`y` sync, `b` new branch, `t` save stash, `g` create PR, and `e` explain output
in chat. Views refresh after operations; use Refresh for changes made externally.
Ahead/behind counts reflect the last fetch. Diffs are previews; staging includes
the entire selected file, including any preview-truncated content.

Unified diffs show green additions, red removals, old/new line numbers, file and
hunk headings, and a change-count summary. Long lines wrap with aligned gutters;
narrow panes omit the number gutter to leave room for the actual changes.

## Recipes

Click Save Recipe and replace `NAME`, or enter:

```text
/recipe save large-files find {{directory}} -type f -size +100M
/recipe set large-files platform any
/recipe set large-files requirements find
/recipe set large-files verification Re-run the listing after cleanup
/recipe show large-files
/recipe run large-files 1 directory=~/Downloads
```

The selected step loads into the editor; Enter executes it. Use `/run` instead of
`/cmd` for terminal handoff. Parameters are shell-quoted, unquoted whole words;
do not wrap `{{directory}}` in quotes or embed it in another word. Separate steps
with a line containing `---` using Alt+Enter to create multiline input. Run one
numbered step at a time. `/recipe verify NAME` asks for a verification command.
`/recipes` lists names; `/recipe delete NAME` removes one. Requirements are
comma/space-separated executable names. Platforms are `any`, `linux` or `macos`.

## Storage and clipboard

State lives in `~/.ai-tool` or `$AI_TOOL_WORKDIR`: saved sessions, `settings.json`,
`recipes.json` and `output/*.log`. Full captured output logs have mode 0600 and are
retained until you remove them; deleting a session does not remove its logs.
Interactive screen contents are not captured. Existing sessions load with defaults
for new fields and interrupted commands are never rerun on restore.

Clipboard uses pbcopy/pbpaste on macOS, wl-copy/wl-paste on Wayland, then xclip or
xsel. Install the matching backend if unavailable.

Verification uses syntax checks and manual smoke checks.

## Panels menu

The orange header has one **Panels** dropdown for Chat, Git, Network, Services,
Watches, Files, Models, and Sessions. Click it from any screen, or type `/panels`
in chat. Up/Down or Tab selects a destination; Enter opens it; Esc dismisses it.
Switching panels preserves your chat draft. Finish or cancel an active panel job
before switching. The header shows active-watch and alert counts when present.

## Watches

Open **Panels > Watches** or `/watch`. Choose Ports, Processes, Disk, or Files,
then **Add Watch**. Forms configure host/port and reachable/unreachable, a PID
exit, a filesystem usage threshold, or literal text in selected files. Process, filesystem,
and file pickers avoid needing command syntax; use **Choose...** or Ctrl+O from
the corresponding draft. Directly entered paths resolve against the session
working directory and support `~`.

Set the check interval, stop-after duration, and notify-once/repeat option. Space
toggles condition and repeat fields; Tab moves between fields. **Apply** starts
checks. Ports make TCP connections without application data; disk checks are
read-only; process checks detect exit/PID reuse. File watches have no default
filename: choose up to 32 files, toggle selections with Space/Enter, then choose
**Use selected files** and Apply. A match in any chosen file triggers the watch,
including text already present when it starts. Matching is literal and case-sensitive.
The observation identifies the file and matching excerpt. Changed or replaced
files are rescanned. An error is displayed, never treated as a match.
Checks happen at the configured interval, not instantaneously. Each file scan
advances by at most 64 KiB per check, so large files can take several checks.

Watches continue while another panel is open. Inspect shows timestamped trigger
evidence; Pause, Resume, Edit, and Remove require explicit confirmation. Repeat
fires when a condition changes from not matched to matched, not every poll.
Ask AI attaches evidence to chat for your next question, without sending it
automatically. **Watch This** in relevant inspectors and `w` in the Files picker
open a prefilled draft. Verify a prefilled port endpoint before applying.

Definitions and bounded evidence are stored in `~/.ai-tool/watches.json` (or
`$AI_TOOL_WORKDIR/watches.json`) with mode 0600. Saved watches always reopen
paused; Resume resets their timeout and baseline. Up to 100 watches are allowed,
with at most 20 trigger events each. File evidence can contain sensitive text.
Polling is best effort, not a substitute for production monitoring; very short
events between checks may be missed. Process identity starts at the first check.

## Services

Open **Panels > Services** or `/services` to browse service state and details.
On macOS, tabs show current-user launchd jobs and Homebrew services. On Linux,
tabs show loaded user/system systemd service units, including inactive units;
this is not a list of every installed service file. Tools reports discovery
failures, permissions, and missing managers.

Select a service and Inspect it. Details appear below the full-width service
list, not beside service names.
Details are hidden initially; **Hide details** or Esc restores the full list.
Start, Stop, and Restart actions **prepare a `/run` command in chat**, never
execute it immediately.
Review and press Enter; authentication stays in the terminal. Refresh Services
after execution. Linux system actions use sudo; user and Homebrew actions do
not. launchd actions require successful inspection of the current GUI job first;
Stop sends SIGTERM, and KeepAlive may restart it automatically. System launchd
management is deliberately not offered.

## Guided investigations

Start a focused TCP-connectivity investigation with `/investigate localhost:3000`
or `/investigate Why is localhost:3000 unreachable?`. Supply exactly one explicit
host and port; bracket IPv6 addresses, for example `[::1]:3000`.

**Next Check** (or `/investigate next`) loads one curated command into the editor.
Review it and press Enter to execute. The sequence checks OS name resolution,
attempts TCP connections, then queries local listeners with `lsof` for loopback
targets. DNS/TCP checks contact the specified target; no application data is sent.
Listener visibility depends on permissions. Failed/unavailable checks remain
evidence, not proof that a service is down. No check or fix runs automatically.

**Evidence** (or `/investigate`) shows captured observations and transcript-turn
references. **Conclude** (or `/investigate conclude`) asks the selected local model
for a likely cause, uncertainty, supported fixes, and a verification check, citing
`[E#]` evidence IDs. These IDs match transcript turn numbers; full output remains
in the transcript. The model receives short excerpts of the latest result for
each check, not an unbounded investigation history. Conclusions can be wrong.

Investigation state and evidence survive reopening a session; interrupted checks
never restart automatically. Rerunning an unchanged curated command updates the
evidence. Start a new investigation to repeat the guided sequence after changes.
`/investigate off` detaches the workflow; `/topic` also clears it. Existing output
remains in the transcript. This version diagnoses TCP reachability only, not HTTP,
TLS, database health, or arbitrary machine failures.

## Network browser

Open **Panels > Network** in the header or `/network`. Supports **SMB**, **NFS**, and
**SFTP/SSH** using installed system tools. Discovery progressively lists advertised
services when you choose **Discover**; opening the panel does not start a scan.
Server/share/mount results are retained when reopening it during the same app
session. Use **Refresh mounts** after a connection, or **Discover** to update
servers. macOS Bonjour service-family queries run concurrently.
Discovery includes
local mDNS services (Bonjour `dns-sd` on macOS, `avahi-browse` on Linux). It also
checks TCP ports 445/2049/22 on up to 128 private IPv4 hosts already present in
the OS network-neighbor table. These results are labelled as open-port observations;
they do not verify protocol support, authentication, or share access. It does
not enumerate IP ranges; other servers can be added manually:

```text
/network smb://nas.local/Public
/network nfs://server.local/export
/network sftp://alice@server.local:22/home/alice
```

Use the four tabs: **Servers**, **Shares / folders**, **Mounted**, and **Tools**.
Select an item, then use an action; Tab switches focus, arrows select, Enter
activates, Esc closes, and Ctrl+C cancels discovery/listing.
In **Shares / folders**, click an SMB share or select it and press Enter to
mount it on macOS. Complete the native login dialog if prompted. The panel
switches to **Mounted**, selects the local folder, and shows its path for file
copy destinations. Click/Enter again to browse. An existing mount for the same
server/share is reused. On Linux this opens the mount-directory form instead.
Clickable controls and rows highlight when the mouse hovers over them in terminals
that support mouse-motion reporting. Hovering does not activate or select them.
On macOS, Bonjour discovery uses terminal-backed capture so `dns-sd` results are
not lost to output buffering; allow a few seconds for each service family.

- **List / inspect** lists SMB shares, NFS exports, or SFTP directories. SMB
  discovery/listing uses anonymous access; restricted shares can be entered by
  URL. SFTP listing uses existing keys/agent and known hosts. **Connect** opens
  Finder's SMB login/share-selection dialog on macOS, `smbclient` on Linux,
  or the system SFTP client for SFTP. macOS owns SMB passwords and Keychain
  integration; the app never receives them. Once Finder has mounted a share,
  return to `/network`, choose **Refresh mounts**, then **Browse mount**.
  The `open` command's exit status only reports launching Finder, not whether
  login or mounting succeeded. Authentication failures are shown by macOS.
- **Mount** asks for an existing empty local directory and optional username.
  For SMB it also asks for the share name (prefilled when a share is selected),
  so mounting a discovered server does not require successful share enumeration.
  It prepares `/run` with the platform mount command. Review it and press Enter
  to authenticate in the terminal. **Unmount** also prepares a reviewable command.
  After returning, reopen `/network`, **Refresh mounts**, select the mount, and
  **Browse mount** to open its files in the regular output inspector.
- **SSH shell** prepares an interactive SSH session for a selected SFTP server.
- **Ask AI** attaches the selected server/share/mount and the last query results
  to the visible prompt context, then returns to chat for your question.

SMB mounting uses `mount_smbfs` on macOS or `mount -t cifs` on Linux (`cifs-utils`).
SMB listing uses `smbutil` or `smbclient`. NFS export listing needs `showmount`
(`nfs-common`/`nfs-utils`); NFSv4-only servers may require a manually entered
export. SFTP needs the system OpenSSH client; mounting additionally needs SSHFS
(and macFUSE on macOS). The Tools tab shows installed commands. Passwords belong
in system prompts, not server URLs; the browser does not store credentials.

## Extract archives from the file inspector

Select a ZIP, tar, `.tar.gz`/`.tgz`, `.tar.bz2`, or `.tar.xz` file in an output
file inspector to browse its contents. Choose **Extract archive** to prepare a
command, replace `{{destination}}` with a shell-quoted destination path, and press
Enter. In the **Files** picker, select an archive and press **x** for the same
extraction workflow. This also works for files on mounted network shares.

Whole-archive extraction uses the bundled standard-library helper
`archive_extract.py`; it creates the destination, refuses existing files, and
rejects links, special files, and paths escaping the destination. An error can
leave files already extracted in place. Selected-file extraction actions in the
archive inspector still prepare the existing system-tool commands.
