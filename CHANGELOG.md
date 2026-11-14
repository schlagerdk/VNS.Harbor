# Changelog

All notable changes to VNS Harbor are listed here, newest first.

## [1.0.4] - 2026-11-14

### Features

- The VNS Harbor dashboard in Umbraco (VNS.Harbor 18.3.1.4) shows which way a sync goes, such as Production → Staging, on each environment and in the sync as it runs, and has Sync everything (settings, then data) where both may go

## [1.0.3] - 2026-11-14

### Fixes

- Update in the Servers window brings the other server up to the release, as an update there does: its sites, its admin, and the old installation removed when nothing uses it
- Roll back is only in the Servers window, also for this server, so it is not clicked by mistake in the settings menu
- The VNS Harbor dashboard is fetched again with each version of the package (VNS.Harbor 18.3.1.2): a browser kept the old one for days and showed sync buttons Sync setup does not allow

## [1.0.2] - 2026-11-14

### Features

- Roll back from the admin: Roll back to … in the settings menu, and for each server in the Servers window. The admin starts again on the earlier release; a release from before VNS Harbor is never gone back to

### Fixes

- The SQL check a site runs before it starts is moved from the old installation to /opt/harbor/current, so the old installation is removed after the update; nothing is restarted for it
- A new site's SQL check runs from /opt/harbor/current, not from one release, so it is there after older releases are cleaned up
- The VNS Harbor dashboard in Umbraco names the main site by the environment it runs as (such as Development), not Main
- The screenshots in the README are in the order they are used

## [1.0.1] - 2026-11-14

### Features

- Scheduled jobs: root's crontab in the Server tools, one job at a time, with when it runs written out in words
- Server tools in one column: SQL Server, the SQL tools and the sa password in one card; an older .NET can be removed when no site uses it
- SQL Server can be installed on a newer Ubuntu than Microsoft supports, with the packages for 24.04, after a warning
- harbor self uninstall takes Harbor off a server and leaves the sites running; its state and secrets are kept in an archive in /root
- A new server's Ubuntu default page in /var/www/html is removed, so it does not look like a site
- Screenshots in the README

### Fixes

- The old installation is removed after an update, also when a service file mentions it in a comment
- An update started from the old installation no longer ends with an error
- Backups are turned off when SQL Server or the SQL tools are removed, and cannot be turned on without them
- A server added again after a reinstall is reached: its old host key is forgotten
- Deleting the databases with SQL Server asks for DELETE as the other confirmations do; a disabled button looks disabled
- The users window: SFTP stands on its own, and "Environments added later" is chosen beside the environments
- An environment's rows: Run custom script stands at the right, and the space above and below the buttons is the same

## [1.0.0] - 2026-11-13

The first release of VNS Harbor.

### Features

- Harbor and the admin run from /opt/harbor: the admin is part of each release (its Python in /opt/harbor/venv, its data where it was), so an update or a rollback is always the whole of it
- A new server is set up with install.sh: the base setup, the newest release, harbor server init (Apache, acme.sh, .NET, keep-alive) and harbor admin install
- The update moves a server to Harbor in full: every site migrated, the admin and the keep-alive job on the release, and the old installation backed up and removed once nothing uses it
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
