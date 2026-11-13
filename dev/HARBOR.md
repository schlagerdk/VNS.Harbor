# VNS Harbor: from scripts to an application

Where the work stands, so it can go on from here after any interruption.

## Decisions
- Name: **VNS Harbor**, command `harbor`, repository renamed from VNS.Linux in phase 3.
- Python (3.10+, standard library only in `harbor/`), installed in `/opt/harbor` from GitHub release tarballs, updated from the admin.
- Other servers: a choice of agent only (no UI) or the full admin.
- Nothing changes for a user until phase 6; every step is a normal release; existing servers update without loss.
- **No terminals for actions (added 2026-11-13):** every action that opens a terminal today gets a clean, premium modal
  with its steps and progress instead. The terminal output is kept, and shown only when there are errors or warnings
  (or when asked for). Commands report steps through `System` (`system.step(...)`, `system.warn(...)`), as JSON lines
  when the admin runs them (`HARBOR_PROGRESS=json`), and as plain text in a terminal, as today.

## Phases
1. Logic of `apache/*.sh` and `sql/*.sh` into the `harbor` package; the scripts become `exec harbor …` wrappers.
2. State out of site folders: `/var/lib/harbor`, secrets in `/etc/harbor/secrets`; read both, write the new.
3. `/opt/harbor` with versioned releases and a `current` link, `install-server-admin`, updater, admin moves into the package.
4. Server tools in the admin: SQL Server, SQL tools, sa, cron, Node.js, .NET.
5. Other servers: agent or full admin, installed and updated from the admin over SSH, version handshake.
6. Migration: dry run, import, verify, generated site scripts to a backup, rollback.
7. 2.0: the `/vns` wrappers go.
8. **NuGet package VNS.Harbor for Umbraco 17+** (asked 2026-11-13; the new backoffice only): a "VNS Harbor" dashboard tab in Umbraco's backoffice
   (beside "Welcome to Umbraco", "Redirect URL Management"), where an editor syncs settings or data from the site
   they are in to the environments they choose, with the same rules as the admin (who may send, who may receive, what
   each takes part with), and the steps shown as they run.
   - The site runs as www-data in its own sandbox and must not run anything as root: it asks the Harbor admin over
     HTTPS with a token of its own, and Harbor runs the sync and reports its steps (the same events as the task window).
   - Harbor gives each site a token scoped to its own syncs, written beside its settings when it is published (never in
     appsettings that are synced or in the repository), and an API: the destinations it may sync to, start a sync,
     follow it. The Harbor that manages the site does the sync, over SSH or SMB as today: the servers synced to need
     nothing new. Only an editor in a site on another server needs that site to reach the managing Harbor over HTTPS;
     from the main site it is local.
   - Who may use it in Umbraco: a permission on Umbraco user groups, off by default.
   - The tab first checks that it reaches the managing Harbor. If it does not, it says the site needs access to Harbor
     at its address over HTTPS to sync from here, and the sync buttons stay off.
   - Depends on phase 2 (settings and the token out of the site folder). Not on phase 5.

## The cutover, when everything is done and works (asked 2026-11-13)
In this order:
1. Bump VNS.Linux to its next version and release it. That release is the bridge: when an existing server runs its
   update, it installs Harbor, updates every site, removes the system scripts from each site's `scripts` folder (the
   site's own custom scripts stay), and deletes the `scripts` folder when it is then empty.
2. Delete everything old from the repository (the scripts, `/vns` wrappers and what only they used).
3. Rename this folder from VNS.Linux to VNS.Harbor and link it to https://github.com/schlagerdk/VNS.Harbor.
4. Release VNS Harbor 1.0.0 from there. Every server then runs Harbor 1.0.0.
5. Only then is VNS.Linux deleted on GitHub (by Martin), once every server has taken the bridge release.

## Phase 1 steps
- [x] Package skeleton: `harbor/`, `bin/harbor`, `harbor/core/system.py` (System, RecordingSystem), `harbor/cli.py`
- [x] Tests on Linux: `dev/test.sh` (linux-01, user vns-test), `.github/workflows/tests.yml`
- [x] Test server deploy without a release: `dev/deploy.sh` (linux-01)
- [x] `set_servername.sh` → `harbor server set-servername` (parity tests in tests/test_server_apache.py)
- [x] `lib/domain_names.py` → `harbor/core/names.py` (old file re-exports)
- [x] 1a `harbor/core/bindings.py`: bindings, aliases, certificate domains, project and publish folder names, exclude_from_build (tests/test_core_bindings.py)
- [x] 1a `harbor/core/templates.py` (render, set_site_vars) and `harbor/core/ports.py` (allocate_ports, same lock and reservations) (tests/test_core_templates.py)
- [x] 1a `harbor/core/site_config.py`: Apache config (HTTP only too), service, HTTPS, Umbraco HTTPS settings, default site, certificates with aliases, production TLS config (tests/test_core_site_config.py). Left for the CLI: ask, install_scripts, render_to
- [x] 1a `harbor/environments/settings.py` (prod.sh: environments, settings read as bash reads them, SSH), `harbor/core/dotnet.py`, `harbor/environments/windows.py` (tests/test_environments.py)
- [x] Task window: `System.step/warn` send OSC 7777 events with HARBOR_PROGRESS=1 (set for every admin task); the admin shows actions (auto_close tasks, not shells) in `#dlg-task` with steps, or a script's latest line, the output folded out on failure or warnings, and closing by itself on success. Ported commands should use `system.step` for their main steps.
- [ ] Try the task window on linux-01 with real actions (user)
- [x] Check service is a status window: `harbor/sites/service.py` (systemctl show + journalctl, here or over SSH), `/api/sites/<name>/service`, `#dlg-service` (tests/test_sites_service.py)
- [x] 1b install_tls_policy, install_apache_conf, fix_permissions → `harbor server install-tls-policy`, `harbor server install-apache-conf`, `harbor site fix-permissions` (tests/test_server_install.py); run for real by update.sh on linux-01
- [x] 1b test_action, prod_action, windows_file(s) → `harbor site action`, `harbor env action`, `harbor env windows-file(s)` (tests/test_actions.py); what runs on another server is sent word for word as before
- [x] Processes apart from Terminals in the header: the server marks a session as a process (`terminal_process`); actions are listed under Processes and open their window again; Terminals shows only shells
- [x] 1c copy_site → `harbor site copy` (tests/test_sites_copy.py, on the shared fake server in tests/harness.py)
- [x] 1c setup_site → `harbor site setup` (tests/test_sites_setup.py)
- [x] 1c delete_site → `harbor site delete` (tests/test_sites_delete.py)
- [x] 1c change_binding → `harbor site binding` (tests/test_sites_binding.py); old tests that read a converted script use `script_text()`, which prefers tests/legacy
- [x] 1c change_binding_prod → `harbor env binding` (tests/test_env_binding.py); the script sent to the environment stays word for word
- [x] 1c create_site → `harbor site create` (tests/test_sites_create.py); the admin reads the default Umbraco version from harbor/sites/create.py; edits keep a file's line endings (the bundled views have CRLF)
- [x] Design: the Protocol select in Add environment is the HTTPS option card; Environment is four choices in its colours (New site, Add environment, Set up site)
- [x] Copy site on this server can move: Remove it here afterwards (+ Also delete the old folder) and Stop the site here while copying, as for another server; `harbor site copy --remove-source config|all --stop-source`; only a site without environments can be moved, and only by a user who may delete it
- [ ] Design: other native selects (Build Debug/Release and the like) the same way, when they come up
- [x] 1d register_env → `harbor env register` (tests/test_env_register.py); create_prod.sh (a site template) still writes the settings, run as before
- [x] 1d remote_site → `harbor env remote` (tests/test_env_remote.py); values sent are quoted for any POSIX shell (shlex) instead of bash's %q, compared word by word
- [x] 1d adopt_site → `harbor site adopt` / `harbor site release`, carry_snippets → `harbor env carry-snippets` (tests/test_sites_adopt.py)
- [x] 1d sync_env → `harbor env sync` (tests/test_env_sync.py, rsync run for real on both sides); `Site` honours VNS_WEB_ROOT as common.sh does (a test hook)
- [x] 1d data_sync → `harbor env data-sync` (tests/test_env_data.py); it uses apache/lib/data_sync.py's functions directly and runs the settings and data syncs in-process
- [ ] 1d move_env, move_site
- [ ] 1e sql/*

## How a script is moved
1. Freeze the script as it is in `tests/legacy/` (and `tests/legacy/lib/` for what it sources).
2. Write the command in `harbor/`, reaching the server only through `System`.
3. Parity test: the frozen script and the command against the same fake root give the same files, commands and output.
   A difference on purpose is a test of its own that says why.
4. The script becomes `exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" <group> <command> "$@"`.
5. `dev/test.sh` (all tests on Linux), `dev/deploy.sh`, and the command run on linux-01.

## Notes
- `apache/lib/domain_names.py` now runs `harbor` from beside the apache folder. A test that puts the apache folder
  elsewhere must link `lib/domain_names.py` to the real one (as the existing tests now do), or copy `harbor` beside it.
- The script ran programs with sudo; harbor runs as root and does not. Parity tests compare without the sudo.
- An older test that runs a script which is now a wrapper runs the frozen copy in `tests/legacy/` instead: it tests the
  behaviour that was, and the parity tests show harbor has it.

- The harness (tests/harness.py) must redirect every server path a script writes or deletes. /var/log/apache2 and
  /root/.acme.sh were missing: the frozen delete_site.sh tried the real ones, and only the unprivileged test user stopped it.

## Test baseline
On linux-01 (Linux, Node) 6 tests fail, the same as v1.1.13 before this work:
test_database_info (3), test_editor_models (1), test_package_updates (1), test_terminal_completion (1).
