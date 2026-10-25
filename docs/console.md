# Operator console

Double-click **Signal Court.bat** at the repository root. It activates `.venv`, starts a hidden server on
`http://127.0.0.1:8765`, waits for HTTP, then opens Chrome (or the default browser if Chrome is unavailable).
Double-clicking again reuses the running console. Git Bash users can run `bash scripts/console.sh`.
Initial setup requires the existing environment (`uv sync`), `.env` and prewarmed `workspace/`.
The isolated console checkout uses local junctions to the original checkout's environment and workspace.

The header says **Warming up…** while the background worker imports the pipeline and touches the document
store. Wait for **Ready**. This absorbs the initial Windows import cost before the stage demonstration.
The server can also be started with `python -m signalcourt.web`. It always binds to loopback.

Choose a company in the left rail. Targets and names come from the local prewarm CSV and universe database.
Search filters targets; **Resolve** or **Enter** resolves other listed names and offers candidates when ambiguous.
**Warm** uses cached filings and checks for fresh data. **Replay** uses cached inputs with no network calls.
A green dot means a completed report with all real stages exists for that symbol; it is a cache-readiness hint,
not a guarantee that every provider response needed by a future replay is cached.

Click **Run research**. Seven tiles show the real stage implementation, record count and elapsed seconds.
The run clock tracks the 180-second presentation budget; it does not truncate the pipeline's configured budgets.
Court proceedings show investigator, red-team and advocate notes as the pipeline emits them.
A failed stage shows its reason and offers **Re-run in replay**. Only one pipeline run can execute at a time.
Refreshing the page reconnects to an active run and restores its event history. A disconnected tab does not
cancel the worker. After a server restart, completed reports remain in **Past runs**.

Completed research opens a full-width report. **Open in new tab** opens its standalone HTML.
**Verify ledger** recomputes the hash chain and shows OK/FAIL, record count and full head hash.
**Download PDF** uses the existing Chrome exporter. If Chrome is missing, the page says how to recover;
`CHROME_PATH` can select an installed browser. **New run** returns to the stage view.
**Past runs** lists saved runs newest first with grades, times and mode. Incomplete runs cannot open a report.
Keyboard: **/** focuses search; **Enter** starts research from the search or main run view.

Server logs live in ignored `workspace/console/`. Run directories and replay data remain in the existing workspace.
To stop the hidden server, close its Python process in Task Manager; stopping it during a run interrupts that run.
No frontend assets require a CDN. Typography falls back to Segoe UI and Consolas when the report fonts are absent.
