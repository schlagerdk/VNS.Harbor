# Changelog

All notable changes to VNS Harbor are listed here, newest first.

## [1.0.10] - 2026-11-16

### Features

- Import from Windows (Tools menu, or harbor windows): sites on a live Windows server are moved here while it keeps running them. A site with a project is copied without its builds, built here and set up as Production; static files (a CDN, a plain HTML site) are copied as they are. Each runs on HTTP with its own binding, and a temporary alias to test it by. A site with only a build cannot be built here and is not imported
- Refresh from Windows, for one site or all: the live data is fetched again (the SQLite database, media and uSync; all files of a static site) as often as wanted. Pause the site on Windows takes it offline (app_offline.htm) while its database is copied
- Take over, once the address has moved: the temporary aliases go, a certificate is issued and the site runs on HTTPS. Nothing on Windows is changed

### Fixes

- The Sync dialogs list only the recent syncs of their own kind: data in Sync data, settings in Sync settings
- Copy site: the options for this server are only shown when copying to this server (Stop the site here was shown twice), and an empty block of options takes no room
- The last mentions of the old installation are named as Harbor or as the old installation

## [1.0.9] - 2026-11-16

### Changes

- Users have a name beside their username, set in Profile (or by an administrator in Users). The menu and the sync history show it, and a sync started in the Harbor admin is named as one from Umbraco is: who, and from where (Anna Hansen (VNS Harbor)). From now on the history names a Harbor user as they are named now: a name set or changed later shows on their syncs too
- What is up to date has no button in the dashboard in Umbraco, only a line that says so and Sync … anyway (VNS.Harbor 18.3.1.7)

## [1.0.8] - 2026-11-16

### Features

- Sync history: Harbor keeps each sync (when, which way, what, who started it and whether it worked), and shows for each environment when settings, content and media were last synced there, and whether the source has changed since: Up to date, Changes not synced, Never synced or Last sync failed. In the admin's Sync dialogs and in the dashboard in Umbraco (VNS.Harbor 18.3.1.6), with the newest syncs
- What is up to date is not synced again by mistake: its button is off, with Nothing to sync, and Sync anyway when something was changed on the destination
- A site that only receives shows its tab in Umbraco too, with Receives from: when it last received, and what has not reached it yet
- A sync in Umbraco closes by itself when it went well, unless Cancel stops it, and has a Close button

### Changes

- Sync data sends only content and media. It waits for the settings only when the content or media that changed uses settings that have changed since they were synced there: a page on a new template, or a document type or data type a changed page uses (also through compositions and block element types). A new data type nobody uses holds nothing back. The reason is shown, such as "Offer uses the template Campaign". Sync everything (or harbor env data-sync --settings-first) sends the settings first. The rule holds in the admin, Umbraco and the CLI alike
- A sync's steps name the environments as the admin does: Production, not production
- The dashboard in Umbraco is called VNS Harbor Sync, its tab and its user group too; the package keeps its name. A user group made before keeps its name and alias
- What this site sends and receives is written with capitals: Settings · Content · Media, or Nothing

## [1.0.7] - 2026-11-14

### Fixes

- An environment is built as its main site would be: Development and Test as Debug, Staging and Production as Release (every environment was built as Release), also on Windows
- Publish all publishes in the order the work goes: the main site, then Development, Test, Staging and Production (it went by the environments' names)
- Each publish says which site it is building and as what, such as "Publish staging-shop.dk (Staging, Release)"

## [1.0.6] - 2026-11-14

### Features

- The VNS Harbor dashboard in Umbraco (VNS.Harbor 18.3.1.5) shows what this site itself sends and receives, with a warning when other environments may overwrite it, and Sync to above the environments it syncs to

### Fixes

- A build that names a package version uploaded minutes ago reads NuGet's package lists anew, instead of failing with "Unable to find package" until NuGet's cache runs out

## [1.0.5] - 2026-11-14

### Features

- The VNS Harbor tab (VNS.Harbor 18.3.1.4) is only in the Content section when there is something to sync: with nothing Sync setup lets the site send, it is not shown. A site Harbor does not answer for keeps the tab, so the dashboard can tell why

## [1.0.4] - 2026-11-14

### Features

- The VNS Harbor dashboard in Umbraco (VNS.Harbor 18.3.1.3) shows which way a sync goes, such as Production → Staging, on each environment and in the sync as it runs, and has Sync everything (settings, then data) where both may go

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
