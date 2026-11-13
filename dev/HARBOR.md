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
8. **Users on the server** (asked 2026-11-13; after phase 4, or right after phase 3): each admin user gets a Linux user
   (no sudo), made and removed with the user. Files access to a site becomes access to its folder through a group per
   site and ACLs (the site keeps running as www-data, owners stay). Grants and revokes in the admin update them at once.
   - The terminal of a user who is not an administrator runs as that user, so it reaches only what they may.
   - SFTP: OpenSSH's internal-sftp for these users only (Match Group vns-sftp, ForceCommand internal-sftp), locked to a
     folder that holds only their sites (bind mounts), password or key managed in the admin, on/off per user and server.
   - Depends on phase 2: scripts, .vns-publish and settings with passwords must be out of the site folders first.
9. **NuGet package VNS.Harbor for Umbraco 17+** (asked 2026-11-13; the new backoffice only): a "VNS Harbor" dashboard tab in Umbraco's backoffice
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
   - **Automatic login to the backoffice** (asked 2026-11-13): the admin's Backend button logs straight in. On the site
     one chooses which Umbraco user that is; no Umbraco password is stored or sent. A click makes Harbor issue a
     one-time token that lives for seconds, signed with a key only Harbor and the site have; the package checks it,
     signs that user in with Umbraco's own sign-in and opens /umbraco. Only users with access to the site in Harbor get
     the button, and each login is logged (who, when).

## Phase 8 steps (users on the server)
Harbor only ever touches the Linux users in its group `harbor-users`; other users of the server are left alone. Which
admin user has which Linux user is kept in /var/lib/harbor/users.json. Passwords stay locked until SFTP (8d).
- [x] 8a `harbor users sync` (the admin's users as JSON, or `--config` with the admin's config.json): a Linux user per
  admin user (not root), named from the username (the part before @, made valid; a suffix when the name is taken by a
  user Harbor does not manage), no sudo, its own group and `harbor-users`. A disabled user expires and gets no shell, a
  removed one is deleted with its home. The admin syncs after every change to its users and when it starts, and shows
  each user's Linux name.
  Done 2026-11-13 (tests/test_server_users.py; tried on linux-01: made, disabled and removed, the server's own users
  untouched).
- [x] 8b A group per site (`site-<name>`, shortened with a hash when too long) with ACLs on the site's folder (rwX, default
  ACLs for new files), never on scripts/ or .vns-publish while they hold passwords (until phase 6). A user with the
  Files grant on a site is in its group; grants and revokes change the groups at once.
  Done 2026-11-13 (tests/test_server_users.py; tried on linux-01 on mhs.linux-01.vns.dk: writes in the site and _publish,
  also after fix-permissions, which sets the ACLs again as a publish does; not the script files or .vns-publish; all of
  it gone when the grant is). The admin syncs after each task too, so a site a user creates is theirs at once.
- [x] 8b2 Found on linux-01: the site folders were readable by every user of the server (another site's appsettings.json,
  scripts/). While the server has users, each site's own folder is closed to others (o-rwx on /var/www/<site> only, an
  ACL first gives www-data rX; vns-build keeps its group rights), its mode kept in /var/lib/harbor/closed-sites.json and
  given back when the last user goes. The admin's own folder is left alone. Tried on linux-01: every site answered 200
  closed and open.
- [x] 8c **Terminal** is a grant per site (asked 2026-11-13): it gives Files too, and a shell in the site's folder as the
  user's own Linux user (runuser, a clean environment), so it reaches what that user may: the site's files, deleting
  them too, never another site, scripts/ or sudo. A shell on another server stays an administrator's (it is root there).
  Tried on linux-01. Note for tests on a server: sync from the admin's config.json, never a list of your own (it
  removed the user "test" Martin had just made; made again at once).
- [ ] 8d SFTP: internal-sftp for the group `harbor-sftp` (Match Group, ForceCommand internal-sftp, ChrootDirectory), a
  folder per user with bind mounts of their sites, password or key set in the admin, on/off per user.

## Phase 4 steps (server tools in the admin)
One "Server tools" window (Server menu) shows what the server has and does each action as a task with its steps. The
terminal scripts in sql/ keep asking as they do; the admin gives the answers as options and the passwords in the task's
environment (never on the command line).
- [x] 4a `harbor server status` (JSON): Ubuntu, SQL Server (installed, version, running), sqlcmd, whether the sa password
  is set in sql/, the backup and log shrink cron jobs, .NET SDKs and ASP.NET Core runtimes, Node.js.
- [x] 4b SQL Server, sqlcmd and the sa password without questions: `--edition`, `--with-tools`, `--yes`,
  `--delete-data`, the password from `HARBOR_SA_PASSWORD`.
- [x] 4c `harbor server cron on|off`: the backup and log shrink jobs added or removed once (the custom cron jobs stay).
- [x] 4d `harbor server dotnet install|update <channel>` and `harbor server node install <major>` (NodeSource).
- [x] 4e The Server tools window in the admin: a card per part with its state and actions, a dialog per action, the task
  window for its steps.
  Done 2026-11-13 (tests/test_server_tools.py, apache/tests/test_server_tools.py; tried on linux-01: status, cron on/off,
  .NET, a wrong sa password refused). The old "Install SQL tools" button in the SQL view stays until the cutover.

## Phase 3 steps (Harbor installed as releases)
/vns stays as it is until phase 7: its scripts, cron jobs and the sa password in sql/ keep working. Harbor itself moves
to releases that are never changed once installed, so an update can be undone.
- Layout: `/opt/harbor/releases/<version>/` (bin, harbor, apache, sql: everything Harbor runs), `/opt/harbor/current` →
  the release in use, `/usr/local/bin/harbor` → `current/bin/harbor`. The three newest releases are kept.
- [x] 3a `harbor self install <source>` installs a release from a copy of the repository (update.sh runs it after it has
  updated /vns, dev/deploy.sh after its markers: a working copy is copied again every time), `harbor self version`,
  `harbor self releases`, `harbor self rollback` (tests/test_self_install.py; on linux-01 since 2026-11-13). Paths that
  are kept elsewhere come from harbor/core/paths.py: from a release they name /opt/harbor/current.
- 3b and 3c move to the cutover (decided 2026-11-13): until /vns goes, its scripts, the admin and cron run /vns's own copy.
  Running the release from /vns/bin/harbor before then would make a rollback change harbor but not /vns/apache or the
  admin: half a system. At the cutover all of it runs from the release, and a rollback is whole.
- [ ] 3b (at the cutover) /vns/bin/harbor runs the release in /opt/harbor/current when there is one. What harbor renders into a site
  names /opt/harbor/current (never a release, which is removed later); the sql folder with the password stays /vns/sql.
- [ ] 3c The admin runs from the release (its venv in /opt/harbor/venv), still as the site admin.<host>.

## Phase 2 steps (state out of the site folders)
State in a site folder today: `scripts/publish_prod.sh` and `scripts/targets/<id>/publish_prod.sh` (an environment's
settings, its password too, at the top of the script that publishes), `scripts/data_sync.json` (Sync setup),
`.vns-publish/*.json` (publish status, package update checks). The generated scripts still run and read their settings
from themselves, so while they exist the old files stay the truth: Harbor keeps a copy in its own place, always equal,
and reads through one API. When the scripts are gone (2d, removed in phase 6) the new place becomes the truth.
- Layout: `/var/lib/harbor/sites/<site>/environments/<id>.json` (settings without passwords), `.../data_sync.json`,
  `.../publish/<scope>.json`; `/etc/harbor/secrets/sites/<site>/<id>.json` (passwords; folders 0700, files 0600, root).
- [x] 2a `harbor/state`: the store, and `harbor state sync [site]`, which makes Harbor's copy equal to the site folder
  (idempotent; an environment or site that is gone there goes here too). update.sh runs it for every site.
  (tests/test_state_store.py; on linux-01 after the deploy: 4 sites, no passwords there, so no /etc/harbor yet)
- [x] 2b One API for reading: harbor's settings, and the admin (environments, Sync setup, publish status), read the site
  folder while the old file is there, else Harbor's copy. (`settings.values/target_ids`, `script_ids` for the site folder
  alone; the admin's target_ids/parse_prod use them; lib/data_sync.py and lib/publish_status.py fall back too; tests/test_state_read.py)
- [x] 2c Every harbor command and admin action that changes a site's state syncs that site after; a deleted, renamed,
  copied or moved site or environment takes its state along (also to the other server in move_site).
  (harbor's main syncs the sites a command touched, all of them after `site receive`; the admin syncs all after each task,
  as root only; lib/data_sync.py and lib/publish_status.py write the copy as they write the site folder. Sync reads the
  site folder only, never its own copy. Tried on linux-01: a changed setting is in the copy at once.)
- [x] 2d The site scripts' work into harbor commands (publish, sync, clear logs, change environment, service tools), so
  nothing needs the files in the site folder. The scripts there then only call harbor; phase 6 removes them.
  - [x] The tools: check/restart/edit service, edit conf, restart Apache, clear logs, update packages and app permissions
    (Python), issue the certificate again, outdated, change environment, publish (`harbor site …`), and an environment's
    clear logs (`harbor env clear-logs`; its script no longer holds a password). The templates are wrappers with the
    site baked in (tests/test_site_tools.py against the templates as they were, tests/legacy/solutionfiles).
    update_admin_site.sh leaves a script that calls harbor alone, and upgrades a very old publish.sh to the wrapper
    with its folder and EnvironmentName. A real publish through harbor on linux-01 worked (12 s, status recorded).
  - [x] publish_prod.sh (Linux and Windows) → `harbor env publish` (tests/test_env_publish.py; the Windows publish tests in
    apache/tests/test_windows_prod.py run against the old script and harbor). The settings block stays in the script,
    which only calls harbor. A real publish to linux-01's production environment through harbor worked.
  - [x] create_prod.sh → `harbor env create` (CreateProdTests in apache/tests/test_windows_prod.py run against the old
    script and harbor). A real one on linux-01: a local Test environment made, published and synced in 15 s, then deleted.

- New site defaults (asked 2026-11-13): environment Development and publish folder `_development` (dialog, admin, `harbor site create`).

## Order after phase 2 (2026-11-13)
Phase 9 comes before phases 3–8: it depends only on phase 2, and Martin asked to have it tested on mhs.linux-01.vns.dk.
Phases 3–8 change how servers are installed and updated; they are done with Martin at hand.

## Phase 9 steps (NuGet package VNS.Harbor, Umbraco 17+)
- The package (`nuget/`): App_Plugins/VNS.Harbor (umbraco-package.json, a dashboard as a plain ES module on the
  backoffice's own import map, no build step) and controllers. Who may use it: members of the Umbraco user group
  "VNS Harbor" (alias vnsHarbor), made empty by the package; administrators add who should.
- The site's connection: `vns-harbor.json` in the folder the site runs from (root:www-data 0640), written by Harbor when
  it publishes: Harbor's address, the site and environment, a token for Harbor's API and a key for backoffice logins.
  Never in appsettings or the repository. The token and key are kept in /etc/harbor/secrets/sites/<site>/harbor.json.
- [x] 9a Automatic backoffice login: the admin's Backend button asks Harbor for a link; Harbor signs a one-time token
  (HMAC-SHA256 with the site's login key: site, environment, the Umbraco user chosen for it, expiry 60 s, a nonce) and
  the package's /umbraco/vns-harbor/login checks it, signs the user in with Umbraco's own sign-in and opens /umbraco.
  Each login is logged in Harbor (who, when, as whom).
- [x] 9b The dashboard "VNS Harbor": it asks Harbor (with the site's token) which environments it may sync settings
  and data to, starts a sync, and shows its steps as they come. Without access to Harbor it says that access to Harbor's
  address over HTTPS is needed, and the buttons stay off.
- [x] 9c Harbor's side: the token API (destinations, start, follow), `harbor site connect` writing vns-harbor.json
  (here; other servers later), the Umbraco user per environment in the admin.
- Done and tried on mhs.linux-01.vns.dk (package 17.0.2 from a local feed /srv/vns-nuget, nuget.config in the site):
  the Backend login, the dashboard without and with access, a settings sync to one and to all environments through the
  token API. Environments are always in the order main, Development, Test, Staging, Production (also the sync's steps).
  Only administrators get the automatic login for now (Martin decides whether more should).
- [x] A new Umbraco 17+ site gets VNS.Harbor (Create site); one that cannot be added is told, and the site is made all the
  same. The connection names Harbor's own address: the admin site on the server.
- [ ] 9d Environments on other servers and Windows: vns-harbor.json written there at publish (now: the site and its
  environments on this server); the package on nuget.org.

## Kept as files that can be changed by hand (asked 2026-11-13)
- `apache/solutionfiles/csharp/umbraco/project` (the Umbraco site a new site starts from: Program.cs, the csproj,
  appsettings.json, wwwroot, Views, uSync) changes over time. It stays plain files in Harbor, which anyone can
  overwrite, change, remove or add to, with no code to change. `harbor site create` copies what is there; when Harbor
  moves to /opt/harbor (phase 3), a server can have its own copy that is used instead of the bundled one, and an
  update never overwrites it.
- `sql/clean_umbraco7_db.sh` and `sql/cron/delete_old_2dm_data.sh` (with their .sql) are custom scripts for some
  servers: not part of harbor, kept as they are where they exist (also by the bridge release and the cutover).

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
- [x] 1d move_env → `harbor env move` (tests/test_env_move.py: each case on two fake servers built alike, rsync real, create_prod/publish_prod played by a fake bash); a real copy linux-01 → linux-02 failed cleanly on the network (linux-01 cannot reach linux-02 on SSH yet) and was undone
- [x] 1d move_site → `harbor site move` / `harbor site receive` (tests/test_sites_move.py: two fake servers per side; the script receives with its own --receive, harbor with harbor site receive). Not yet tried for real: linux-01 → linux-02 SSH is blocked in the network (linux-02 → linux-01 internally is open, but needs a login)
- [x] 1e sql/* → `harbor sql …` (tests/test_sql.py); sqlcmd gets the sa password in SQLCMDPASSWORD instead of -P. The wrappers keep the line `sa -U sa -P '…' -i scripts/<x>.sql`, so set_sa_password.sh, install_server.sh and update.sh (which reads drop_db.sh) find and keep the password as before. The custom scripts stay outside harbor

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
