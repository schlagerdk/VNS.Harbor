# Changelog

All notable changes to VNS Harbor are listed here, newest first. Before 1.0.0 it was VNS.Linux, whose versions follow.

## [1.0.0] - 2026-11-13

The first release of VNS Harbor. A server on VNS.Linux 1.3.0 is moved over by its next update.

### Features

- Harbor and the admin run from /opt/harbor: the admin is part of each release (its Python in /opt/harbor/venv, its data where it was), so an update or a rollback is always the whole of it
- A new server is set up with install.sh: the base setup, the newest release, harbor server init (Apache, acme.sh, .NET, keep-alive) and harbor admin install
- The update moves a server off /vns: every site migrated, the admin moved to the release, the keep-alive job on the release, and /vns backed up and removed once nothing names it
- A clean layout: bin/harbor, harbor/, admin/, lib/ and templates/; the old scripts, site templates, update.sh and init.sh are gone
- A new site gets no scripts folder: its tools are harbor commands, and an environment's settings go straight into Harbor
- VNS Harbor has its own mark in the header, on the login and as the icon; the VNS logo is at the bottom as Powered by VNS
- Staging and Production sites are published as Release builds, Development and Test as Debug
- A NuGet package added without a version gets the newest stable one from nuget.org

### Fixes

- A build can write in a site folder that is closed to other users (the build failed to write Umbraco's schema files)
- A publish to an environment brings the packages' own code in wwwroot/App_Plugins (the VNS Harbor dashboard, uSync and VNS.Umbraco updates); the rest of wwwroot stays as it is
- An environment's connection names Harbor's address again, so its VNS Harbor dashboard reaches Harbor
- The Servers window compares versions, so a test build no longer shows an update for a server on the same version
- The terminal in a task's window fits the window, its scrollbar is thin and dark, and the server's addresses have a pointer cursor

# VNS.Linux

## [1.3.0] - 2026-11-13

### Features

- Harbor keeps the sa password itself and the backup and log shrink jobs run harbor, so they no longer need the scripts in /vns/sql; every update moves both over (custom jobs stay as they are)

## [1.2.2] - 2026-11-13

### Fixes

- A server's name in Add server is made lower case with - as it is typed (KIKAYO becomes kikayo), and a name that is not valid says what it must be

## [1.2.1] - 2026-11-13

### Fixes

- 1.2.1: the first update from 1.1.x already installs Harbor and migrates the sites, as the admin's updater does it after the new code is in place (the update.sh that runs is the one the server had)

## [1.2.0] - 2026-11-13

### Features

- SFTP per user: their sites' folders and nothing else, as their own Linux user, with a password or public keys set in the admin; the SSH server gets Harbor's settings only while a user has SFTP, checked before it is reloaded
- Harbor is installed as releases in /opt/harbor (current, the three newest kept, rollback) by every update, and harbor is on the PATH; paths that are kept elsewhere name /opt/harbor/current, never a release
- The site tools (service, Apache config, logs, certificate, packages, environment, publish) and clearing an environment's logs are harbor commands; the site templates only call them, and an environment's clear-logs script no longer holds a password
- Harbor keeps its own copy of the sites' state, passwords apart and readable only by root, and every update brings it up to date; a new site is Development and publishes to _development by default
- Move_env, move_site and the sql scripts now run harbor, tested against the scripts they replace; the sa password stays where set_sa_password.sh and updates keep it, and sqlcmd gets it in its environment
- Register_env, remote_site, adopt_site, carry_snippets, sync_env and data_sync now run harbor, tested against the scripts they replace
- Copy site on this server can also move it, with Remove it here afterwards and Stop the site here while copying, so a site can get a new folder and binding; change_binding, change_binding_prod and create_site now run harbor, tested against the scripts they replace
- Start of VNS Harbor: the harbor package with the shared helpers of lib/common.sh, set_servername.sh as its first command, tests that compare each with the script it replaces and run on Linux, and tools to deploy and test a working copy on a test server
- Copying or migrating to another server can set up HTTPS, with DNS for every site that goes checked against that server; each gets its certificate once it runs there, and one whose DNS does not point there yet stays on HTTP

### Fixes

- The old tests that failed on GitHub match the code again, and the package update test no longer asks NuGet, so a push no longer fails

### Other

- Building for harbor

## [1.1.16] - 2026-11-13

### Features

- Administrators see the server's internal and external address in the header, and a click copies one

## [1.1.15] - 2026-11-13

### Features

- Copy site on the main site is one dialog for this server and another server, where Remove it here afterwards makes it a migration, and its choices and those of the environment dialog are option cards

## [1.1.14] - 2026-11-13

### Features

- The test banner stands out clearly, with a warning icon, the environment name in a dark badge, larger bold text, stronger stripes and a dark bottom edge; it casts a shadow only once it sticks and pushes headers pinned to the top of the page down below it

## [1.1.13] - 2026-11-12

### Features

- A name that resolves only to Cloudflare's addresses counts as pointing here, shown as Via Cloudflare in the DNS check, and does not block a certificate

## [1.1.12] - 2026-11-12

### Features

- The DNS check in Change bindings checks every alias as well, one row per name with the record to make, and the selected grid/list layout is lit in blue

## [1.1.11] - 2026-11-11

### Features

- New site offers HTTPS only when the binding points here, and the server refuses a certificate for a name that does not

## [1.1.10] - 2026-11-11

### Fixes

- The DNS card no longer flashes when New site opens: it appears only once a binding has been checked

## [1.1.9] - 2026-11-11

### Fixes

- An admin update keeps the server's activity log, and the log from development runs is no longer in the repository

### Other

- Style: A name that already points here shows only a larger Points here, without DNS records

## [1.1.8] - 2026-11-11

### Features

- The DNS card's CNAME follows the address the admin is visited on, and a domain itself gets only an A record
- A subdomain's DNS card also suggests a CNAME to the server's own host name

## [1.1.7] - 2026-11-11

### Features

- Check for updates covers the sites shown, DNS records point to the public address, and the admin fits a phone

## [1.1.6] - 2026-11-11

### Fixes

- The update screen waits through the admin site's restart in Safari too, and an update that did not finish is shown in a card with Reload page

## [1.1.5] - 2026-11-11

### Fixes

- The build user may add files of its own to a project folder, as Umbraco's build does, but not change root's

### Other

- Style: May create sites, May add SQL connections and Locked are rows with icons and switches, joined to their card

## [1.1.4] - 2026-11-11

### Fixes

- New site and the grid/list toggle only show when they can be used, and Web Server and SQL Server are cards with switches in the user editor

## [1.1.3] - 2026-11-11

### Fixes

- A user is sent to the part they have access to, and a user without sites is told who gives access

## [1.1.2] - 2026-11-11

### Fixes

- Small dialogs are back as they were, and changing a user's access no longer logs them out

### Other

- Style: The buttons in small dialogs have room and a line above them

## [1.1.1] - 2026-11-11

### Features

- Icons for Users and Activity log, root can clear the log, a nicer Profile, and the user editor lists a connection's databases

## [1.1.0] - 2026-11-11

### Features

- Multiple users with roles, per-site and per-environment permissions, SQL access per connection and database, locked task windows for users, an activity log, and builds as the unprivileged user vns-build

## [1.0.25] - 2026-11-11

### Other

- Style: The header search has its own clear button, larger and at the right edge

## [1.0.24] - 2026-11-11

### Features

- A styled snippet menu, a choice of answer for Allowed IPs, a compression snippet, and X-Powered-By and Apache's version left out on every site
- Allowed IPs updates the rule for the same paths and adds a new rule for other paths

### Other

- Style: The header search matches the section switch, with a search icon and the / shortcut

## [1.0.23] - 2026-11-11

### Fixes

- Snippets ask for all their fields in one window, and an Apache rewrite is sent straight to the site's service so it works with nocanon

## [1.0.22] - 2026-11-11

### Features

- The config editor can insert snippets for web.config and Apache: ignore paths, allowed IPs, trailing slash, remove www, redirect to a domain, rewrites and secure cookies

## [1.0.21] - 2026-11-11

### Features

- A site's environments can be shown as a list with Publish, Clear logs and Sync on each row, unfolding to the whole card

## [1.0.20] - 2026-11-11

### Fixes

- The list view always shows every environment of a site

## [1.0.19] - 2026-11-11

### Fixes

- The overview's buttons stay on the right when there are no filters, e.g. with a single site

## [1.0.18] - 2026-11-11

### Features

- Filters for sites with pending publishes, updates, failed checks, unfinished setup or missing environments

## [1.0.17] - 2026-11-11

### Features

- Windows environments show their datasource, the site list lines up in columns, and the .NET version is plain text on the detail page

## [1.0.16] - 2026-11-11

### Features

- The overview can be shown as a list, .NET badges have a colour per version, Publish all beside a site's title, and a selected card can be clicked anywhere

## [1.0.15] - 2026-11-11

### Features

- Sites show the .NET version they run, in the overview and per environment, also on remote Linux and Windows servers

## [1.0.14] - 2026-11-11

### Fixes

- A Windows site started by its own .exe is recognised when adopted, and a mismatch shows the web.config's start line

## [1.0.13] - 2026-11-11

### Features

- Package check offers only versions the project's .NET supports, and init installs .NET 11 beside 10 and 8

## [1.0.12] - 2026-11-11

### Fixes

- Appsettings saved as Windows-1252 are read, and a failure to make a site wait for SQL Server does not stop its setup

## [1.0.11] - 2026-11-11

### Fixes

- Sites whose setup was not finished can be set up, and a site's page no longer shows the site list first on reload

## [1.0.10] - 2026-11-11

### Features

- Restore database asks for name, login or overwrite confirmation in one dialog

### Fixes

- Prompts can be edited and trimmed, yes/no questions show [Y/n] or [y/N], acme.sh gets its folders in init.sh, and an invalid DNS name shows what was typed

## [1.0.9] - 2026-11-11

### Other

- Style: More space above the keep-alive hint in Preferences

## [1.0.8] - 2026-11-11

### Fixes

- Scripts whose names start with _ are not shown in the custom scripts dropdown

## [1.0.7] - 2026-11-11

### Features

- Log files can be cleared from the logs window, empty files are not shown, and the Server and account menus close each other

## [1.0.6] - 2026-11-11

### Fixes

- The log list only offers the levels the log has feat: The logs window shows the files or journal command it reads, with a heading per file in the Apache logs
- The log list only offers the levels the log has feat: The logs window shows the files or journal command it reads, with a heading per file in the Apache logs

## [1.0.5] - 2026-11-11

### Features

- Clear logs of the main sites, all environments or all in Multi action

## [1.0.4] - 2026-11-11

### Fixes

- The update finds the newest release from the git tags

## [1.0.3] - 2026-11-11

### Features

- Clear logs and Select all in Multi action, and package fields side by side

## [1.0.2] - 2026-11-11

### Features

- Logs as a list, Multi action and keep-alive as cards, Web Server in the header

## [1.0.1] - 2026-11-11

### Features

- A Sites | SQL Server switch in the header, a clearer Server menu and Preferences

## [1.0.0] - 2026-11-11

- The first release with a version number. The changes before it are in the commit history.
