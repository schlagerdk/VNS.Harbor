# Changelog

All notable changes to VNS Harbor are listed here, newest first.

## [1.0.20] - 2026-11-16

### Fixes

- A server added at an address, or moved to one, has its old host key forgotten in root's known_hosts too, not only Harbor's: the environments on it log in as root does, and a copy to a server set up again at the same IP failed with REMOTE HOST IDENTIFICATION HAS CHANGED
- Fix permissions on an environment on another server failed with "No module named domain_names": the helper sent over SSH now carries the names it needs, so it runs on a server with or without Harbor. Edit config there failed the same way (name __file__ is not defined): every helper sent over SSH carries them now
- Backoffice login is not offered on a site managed from another server that does not have the VNS.Harbor package: it can only be added where the site is built. A copy without its project here has the package when its build has VNS.Harbor.dll

## [1.0.19] - 2026-11-16

### Fixes

- The temporary alias suggested in Import from Windows is under the name the admin is reached by (admin.<name>): that name reaches the server now, also while it borrows another server's address. The server's own full name only when the admin is reached by IP
- The temporary alias suggested in Copy from Windows is under the Linux server's admin name that reaches it now: Harbor learns the names its admin is reached by (admin.<name>) with its status, and takes the one whose DNS points to the server, also while it borrows another server's address

## [1.0.18] - 2026-11-16

### Fixes

- Import from Windows and its Refresh no longer print a chown error for a site without a SQLite database, media or uSync on Windows (as one on SQL Server): it says there was no data to fetch
- _Publish and wwwroot/Media from Windows are _publish and wwwroot/media here (Import and Copy from Windows): Windows does not tell the cases apart, Linux does, so a site had its settings in _Publish and ran from _publish. A folder in another case is renamed, or put into the lower case one. Binaries in _Publish are not imported: the site is built here

## [1.0.17] - 2026-11-16

### Features

- Edit on a server in Servers (harbor servers edit, rename, set-host): its name and its address. A new name takes a Windows server's password with it, and the copies from Windows that name it follow. A new address is for a server given another IP, such as one that takes over the Windows server's address. Harbor logs in at the new address, then changes it in Servers and in every environment that was on the old one, so publish, refresh and take over go on there. Nothing changes on the server

## [1.0.16] - 2026-11-16

### Changes

- Import from Windows no longer imports a folder with only a build (Build from is gone): such a site is an environment of a site with its project, and Copy from Windows copies it, built from that project. It is listed, with a note that says so

## [1.0.15] - 2026-11-16

### Fixes

- Copy from Windows and Import from Windows show the temporary alias before the list of sites, and what is copied or imported and not taken over yet (with Refresh all) at the top, so it is seen and checked before the button: the button stays at the bottom while the list scrolls
- The file editor and the SQL query window close with a Close button, as the other windows, not a small x
- A long step in Processes is cut with an ellipsis, without a scrollbar
- The steps of a task window always follow the newest one: when it is opened again, and with each new step even when the list was scrolled up
- Awaiting take over in the overview: the sites copied or imported from Windows that Windows still runs

## [1.0.14] - 2026-11-16

### Changes

- Move from Windows is called Copy from Windows: nothing on Windows is removed or changed, and the site there stays the live one until you take over. The command is still harbor env windows-move

### Fixes

- Copy from Windows (until now Move from Windows) stopped after adding the temporary alias, and Take over after issuing the certificate: a setting of an environment whose settings Harbor keeps (no script in the site folder) was written to a script that is not there. It goes to Harbor's copy, as binding, HTTPS and environment changes

## [1.0.13] - 2026-11-16

### Fixes

- An environment moved from Windows and then deleted no longer counts as moved: the one on Windows can be moved again, and Refresh from Windows leaves it out

## [1.0.12] - 2026-11-16

### Features

- The temporary alias in Move from Windows and Import from Windows is filled in as {binding}.<the server's full name> (as web-02.example.com), from the server chosen or this one. Harbor learns each server's full name with its status; a typed alias is kept

### Fixes

- Move from Windows stopped with an error (AttributeError) when it added the temporary alias, after the environment had been copied and started; Import from Windows had the same fault with aliases, and Take over in both when it removed them. The aliases are given as text, as the binding change reads them

## [1.0.11] - 2026-11-16

### Features

- Windows servers in Servers: a share and its login, kept with the Windows logo (Add server, Windows). Harbor tries the login first, keeps the password for root only, and runs nothing there
- A server from Servers can be chosen wherever another server is asked for: Add environment (Linux and Windows), Copy site to another server, and Import from Windows. In Add environment the list is at the top, under the kind of server. Choosing one hides its fields; a Linux server is reached with Harbor's key, and a Windows password is never sent to the page

### Fixes

- The last site in Import from Windows is no longer cut off at the bottom of the list
- DNS for a server behind a router is checked against its public address: Harbor asks each server in Servers what the internet sees it as, and keeps it, also when a dialog lists the servers before Servers has been opened. One whose public address is not known is not called wrong for pointing to a public address in front of its internal one
- Copying or moving an environment from Windows fetches only the SQLite database from umbraco (umbraco/Data), never its logs, locks or TEMP: a file IIS holds open could not be read, and the copy failed. rclone gets filters, not --include with --exclude
- Move from Windows, Import from Windows, Refresh and Take over run in a task window with their steps, not in a terminal; Move from Windows is only in the menu when a site has an environment on Windows
- A main site with environments has no Delete site: they are built from its project, so they are deleted first, and harbor site delete says so when asked to delete the main site alone
- The tests run on GitHub again (a site's details read the moves from Windows without Path)

## [1.0.10] - 2026-11-16

### Features

- Move from Windows (Tools menu, or harbor env windows-move): every site's environments on a live Windows server are moved to a Linux server Harbor knows, as new environments of their sites. Each is built from the main site's project here, with the settings and files of the one on Windows (appsettings, umbraco, uSync, Views, wwwroot, App_Data), on HTTP with a temporary alias. The one on Windows stays the live one meanwhile
- Import from Windows (Tools menu, or harbor windows), for sites that are not environments here, on the server they move to: a site with a project is built there; a build is built from its project on that server or on one Harbor knows (Build from), and its dlls never come from Windows; static files are copied as they are. A classic ASP.NET site (Umbraco 7 and older) runs on Windows only and is not imported. A filter shows what can be imported, by kind and by name
- Refresh from Windows, for one or all: the live data is fetched again (the SQLite database, media and uSync; all files of a static site), as often as wanted. Pause the site on Windows takes it offline (app_offline.htm) while its database is copied
- Take over, once the address has moved: the temporary alias goes, a certificate is issued and it runs on HTTPS; a moved environment's Windows environment is removed from Harbor. Nothing on Windows is changed
- An environment on a Linux server added in Servers is reached with Harbor's own key: it needs no password of its own

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
