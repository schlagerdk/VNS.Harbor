# VNS Harbor: from scripts to an application

Where the work stands, so it can go on from here after any interruption.

## Decisions
- Name: **VNS Harbor**, command `harbor`, repository renamed from VNS.Linux in phase 3.
- Python (3.10+, standard library only in `harbor/`), installed in `/opt/harbor` from GitHub release tarballs, updated from the admin.
- Other servers: a choice of agent only (no UI) or the full admin.
- Nothing changes for a user until phase 6; every step is a normal release; existing servers update without loss.

## Phases
1. Logic of `apache/*.sh` and `sql/*.sh` into the `harbor` package; the scripts become `exec harbor …` wrappers.
2. State out of site folders: `/var/lib/harbor`, secrets in `/etc/harbor/secrets`; read both, write the new.
3. `/opt/harbor` with versioned releases and a `current` link, `install-server-admin`, updater, admin moves into the package.
4. Server tools in the admin: SQL Server, SQL tools, sa, cron, Node.js, .NET.
5. Other servers: agent or full admin, installed and updated from the admin over SSH, version handshake.
6. Migration: dry run, import, verify, generated site scripts to a backup, rollback.
7. 2.0: the `/vns` wrappers go.

## Phase 1 steps
- [x] Package skeleton: `harbor/`, `bin/harbor`, `harbor/core/system.py` (System, RecordingSystem), `harbor/cli.py`
- [x] Tests on Linux: `dev/test.sh` (linux-01, user vns-test), `.github/workflows/tests.yml`
- [x] Test server deploy without a release: `dev/deploy.sh` (linux-01)
- [x] `set_servername.sh` → `harbor server set-servername` (parity tests in tests/test_server_apache.py)
- [x] `lib/domain_names.py` → `harbor/core/names.py` (old file re-exports)
- [x] 1a `harbor/core/bindings.py`: bindings, aliases, certificate domains, project and publish folder names, exclude_from_build (tests/test_core_bindings.py)
- [x] 1a `harbor/core/templates.py` (render, set_site_vars) and `harbor/core/ports.py` (allocate_ports, same lock and reservations) (tests/test_core_templates.py)
- [x] 1a `harbor/core/site_config.py`: Apache config (HTTP only too), service, HTTPS, Umbraco HTTPS settings, default site, certificates with aliases, production TLS config (tests/test_core_site_config.py). Left for the CLI: ask, install_scripts, render_to
- [ ] 1a `lib/prod.sh`, `lib/dotnet.sh`, `lib/windows_prod.sh`
- [ ] 1b install_tls_policy, install_apache_conf, fix_permissions, windows_file(s), test_action, prod_action
- [ ] 1c create_site, setup_site, delete_site, copy_site, change_binding, change_binding_prod
- [ ] 1d register_env, remote_site, adopt_site, carry_snippets, sync_env, data_sync, move_env, move_site
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

## Test baseline
On linux-01 (Linux, Node) 6 tests fail, the same as v1.1.13 before this work:
test_database_info (3), test_editor_models (1), test_package_updates (1), test_terminal_completion (1).
