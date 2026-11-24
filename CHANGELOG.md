# Changelog

All notable changes to VNS Harbor are listed here, newest first.

## [1.0.72] - 2026-11-24

### Features

- Settings has Usage with every site and database, who made it and its limit (each site and database its own), deleting a user can take their sites and databases with them, the header's parts can be turned off, and the README shows SQL Server and Usage (1.0.71)

## [1.0.71] - 2026-11-24

### Features

- Settings has Usage: every site and every database on each connection, biggest first, with who made it and its limit (red past it), measured as it opens. A name opens the site, or the SQL page on its connection; a user opens their access and limits at the size of what was clicked, its card folded out and the field lit and ready. The usage alerts moved there from General
- Deleting a user who made sites or databases asks whether those go with them, each by choice and named, and deletes them as one task with a step each
- Delete database asks with cards, the logins that go with it named (those used by it alone) and its backup files
- The SQL connection is a card as the others: its icon, where it goes (login @ host:port) under the choice, Edit as an amber pencil and Add connection as a green plus
- Top menu has Header shows: the site search, the server's IP addresses, Files and Terminal in the header, each on or off by each administrator for themselves (open terminals show Terminal anyway)
- A site opened from Settings or the SQL page leads back there (Back to Usage, say), not to All sites, also after a reload
- Only their own on a user's SQL connection ticks every action, as what they make is theirs; each can still be taken off
- A user changed or deleted from Usage shows in it at once
- A user's window has Web Server, SFTP and SQL Server as a part each, framed in its own colour with its switch at the top; SFTP is its own part while the Web Server is theirs, and its SSH keys field no longer resizes
- Create database's Also make a login for it is a card as the other choices, and once a user has made as many databases as they may, Create database goes as the SQL page shows again
- A database a user made can have its own size as well, on its connection under the user, bigger or smaller than every database there; empty, it takes that one
- A site a user made can have its own size, bigger or smaller than the one every site of theirs gets; empty, it takes that one (named in the field). Usage, the warning and the alerts go by it
- A column's type in the table designer is chosen in a dropdown of the page's own (the one chosen ticked, arrows, Enter, Escape and a letter to jump)
- The Logins tab is there as the SQL page opens, its logins read beside the databases with a spinner until they come
- Delete database and Delete user look as every other confirmation that deletes: a red title, no banner, and Delete grey and off until the name is typed as it is, then red; the name to type copies when clicked
- The SQL connection's window has its buttons on one row: Cancel, then Remove, Test connection and Save, each with its icon
- What changed under Updates shows the release notes since the version here in a window, with every change on GitHub a link away

## [1.0.70] - 2026-11-24

### Features

- Settings is a page of its own, beside Web Server and SQL Server (for administrators), opening on General; its menu is Harbor, Servers, This server and Software, with SSH and Samba as one part, Access: SSH and Samba side by side, and Samba's shares and users as lines in two cards beside each other, in place of dialogs opened one from another: a side menu with each tool as a part of its own (SQL Server, .NET, Node.js, SSH, Samba), then Scheduled jobs, Settings, Servers and Users (#server/sql, #server/jobs, …). On a phone the menu is a row above the part
- Users are cards, three beside each other with what each may reach, and a dashed card after them adds one
- The tools are cards, three beside each other with their buttons at the bottom: SQL Server, SQL tools and the sa password a card each, .NET a card each version, Samba a card for itself and, while it is on, each share and each user under their headings (and dashed cards to add a version, a share or a user)
- The backups are in Scheduled jobs: once SQL Server and its tools are here, the backup and the log shrink are each suggested while it is not in cron, and added with a click (harbor server cron on --job); removed, it is suggested again
- Settings are cards too, saved as they are changed: the top menu and an administrator's own start page in one, the public address as it is found (set by hand in a dialog, only where it cannot be), and the keep-alive with Turn on or off and its interval
- Tools, Servers and Users are no longer in the menus; Preferences, the start page, is in the menu only for a user who is not an administrator and has both Web Server and SQL Server
- A job is added or changed in a dialog of its own
- The top menu and start page are a part of Settings of their own (Top menu); General has the public address and the mail server beside each other, keep-alive under them
- Admin site is in the Settings menu under Harbor, no longer in the Tools menu: its page (#admin-site) belongs to Settings, which the top menu shows and its back link leads to
- Windows is a part of Settings under Harbor (Import sites and Copy sites as cards, while there is a Windows server or environment), no longer in the Tools menu
- The admin site's page is shown in Settings, to the right of its menu, with Admin site chosen there
- The Tools button is gone: Processes (while something runs), Files and Terminal are one group at the top, by the account (Terminal opens the terminals open, or a new shell when there are none; + in the terminals' bar opens another), and Updates is a part of Settings under Harbor (the version here, Check for updates, the update with what changed, and Roll back). The version is shown beside the server's name at the top, with Update available beside it when there is one
- Apache is a part of Settings under This server (its config, a test of it, its error logs and a restart, a card each), no longer in the Tools menu
- A Samba share's folder can be chosen with Browse…, through the server's folders
- Keep-alive keeps a log of what is wrong with the sites and environments: not answering, a certificate error, a certificate that expires within 14 days, a service that has failed, and a site stopped while it is live (visitors see the page that says it is updating). Log shows what is wrong now and what broke and was solved lately. The card says when it ran last and runs next
- Test run in the email report's dialog checks every site now and mails a test report to the addresses typed there, shown as it is sent, before anything is saved; Send test in the mail server's dialog tries what is typed there the same way
- Keep-alive's email report: to one address or more, when something breaks (and when it works again), once a day, or both, about the checks chosen. It goes through Harbor's mail server, set up under General for all the mail Harbor sends (SMTP with STARTTLS, SSL or none, its password kept for root only), with Send test
- The page is called Settings in the top menu, with a gear, and its own settings part General; the quick menu of Shell, files, Apache and updates is called Tools, with a terminal icon
- Servers: three beside each other, with a dashed card after them that adds one; another Harbor server has Open admin, its own admin in a new tab
- Settings: what is in the top menu beside Server, one choice of four: Automatic (Web Server while there are sites here, SQL Server while it or the SQL tools are installed here, or there is a SQL connection), both, Web Server only or SQL Server only. The admin opens on the one there is

- SQL Server: tables are made and changed as in Management Studio. New table and Design open a designer with a line per column: its name, a SQL Server type (nvarchar, decimal, datetime2 …) with its length, precision or scale, NULL, a default (a value, or GETDATE(), NEWID() and the like), identity and the primary key. Review SQL shows the SQL it makes (one transaction: renames, retyping, defaults, columns added and dropped, the key moved), and Run runs it. A table's rows open with Select top 1000, and a table is renamed in the designer or dropped (its name typed to confirm). Users are given this as Create and change tables
- SQL Server: the server's logins are a panel of their own, for administrators: a login is added with its password and the databases it may use with their roles (owner, datareader, datawriter, ddladmin, backupoperator), and its databases and roles changed, its password changed, locked and let in again, or deleted (with its user in each database). Its password needs 6 characters: the logins made here are not held to SQL Server's password policy. sa and the connection's own login are left alone
- A password typed in a dialog reaches the server again: the field shows only dots, and the forms sent those dots, so a SQL connection to another server was refused with the right password typed (since 1.0.45, seen now that a connection is tried before it is saved). The eye beside it shows the password itself
- A new table's schema is chosen among the database's own schemas (dbo first) in place of a text field, and Add a column has room under it
- SQL Server's own databases (master, model, msdb, tempdb) are read only in the table list: their tables can be read, not designed, dropped or added to, and the server refuses it too
- A login's whole line opens its databases and roles
- Usage limits: a user who may make sites can be given how many they may make and how big each may grow, and each SQL connection where they may create or copy databases the same of its databases. How many is held when they make, copy or restore to a new one, and New site, Create database and Copy are not shown once they have made as many as they may. Once an hour Harbor measures each site and database a user made, and Usage alerts under Settings, General mails the recipients set there when one grows past its limit, and when it is under again; Check now measures at once and shows every site and database the users made, by user, beside its limit, with what is over in red
- A user past a limit sees a red warning across the top, of the sites on Web Server and of the databases on SQL Server, with what is over and by how much, and See usage; the hourly check runs while a user has a size limit, also with the alerts off. The alert mail lists, under what changed, every site and database the users made with its size and limit
- Usage in the user menu shows a user what they have made and how big each is, beside their limits, as meters (amber near a limit, red past it); an administrator sees the same with Usage on the user's card under Users
- The header's Terminal button is not shown to a user for a moment while the page loads
- Empty lists of databases and logins show a big icon, what goes there and the button that makes the first; their tab counts only the user's own databases
- The SQL page loads with a spinner in the connection's box (and in a database's tables), with no line that pushes the page; Edit connection, Create database and Backup all show only as they will stay, and one connection there is nothing to choose or change on has no box at all, its name by the heading
- A user who is not an administrator sees only the logins of their own databases and those they made, never sa or a sysadmin
- A user's SQL connection gives every database, only their own (those they make, copy or restore to, also before they have made one, with Create databases) or chosen ones, as segments
- Users can be given Logins (create and change them) on a SQL connection: they see the Logins of their own databases, and may change a login only when it reaches none other and is not a sysadmin
- A database's tables folded out under it no longer light up all at once under the pointer
- Logins show a sysadmin as one (every database, in place of the databases whose dbo it happens to be), with a button that takes sysadmin away. A user of a login's name left without it (a database from another server) is shown, and a button links it to the login
- Create database makes a login of its own that owns it, as harbor sql create does (a chip turns it off); a new login's password needs 6 characters, as elsewhere
- The SQL page keeps the connection chosen last, also after a reload, and shows its databases, backups and logins one at a time under tabs with their counts, the one chosen kept. Add connection takes Refresh's place beside the connection, and Add login sits in the Logins header as Create database does in Databases'
- Query sends a typed script to SQL Server only as hex run by sp_executesql: sqlcmd's own commands in it ran (!! ran a shell command as root on the admin server, :r read a file)
- A user who is not an administrator is kept to the database a query is run on: the script runs as its dbo, which cannot revert, so other databases and the server are out of reach; SQL Server's own and TRUSTWORTHY databases are refused them
- A refused SQL login says which password it tried (typed, saved or Harbor's own sa), its length and whether it begins or ends with a blank, without showing it
- A query's columns fit what they hold: ids, GUIDs, numbers and dates are as wide as their values, and text such as a name takes the rest, also while its values are short. The SQL connection's password fields have an eye that shows what is in them
- Query has Snippets: the T-SQL most often written (select, insert, update, delete, count, find, a transaction, create a table, add a column, an index, table sizes, who is connected …), put in at the cursor with the table it was opened on. A change made in a table's rows shows its rows again under it
- Add SQL connection has Test connection, which tries what is typed without saving it, and Save says it is trying to log in while it does, and why not under the fields when it cannot
- The screenshots in the README show the new layout

### Fixes

- The name and the server at the top no longer wrap when the top is full: Files and Terminal show their icons alone below 1400 pixels
- A Samba password needs 6 characters, not 8
- The email report tells of what was wrong before it took it in (turned on, or a check chosen later), once: it told only of what changed after
- A SQL connection is tried before it is saved, and one that cannot log in is not kept (a wrong sa password was saved, and every action then failed with Login failed for user 'sa'). For sa on this server there is no password field: Harbor's own sa password is used (a browser filled the field with another saved password, and the login failed)
- A scheduled job's row no longer jumps when its log is opened, and the log opens once it is read, at its own height
- Add job keeps Cancel and Add job at the bottom while the form scrolls (Browse… open, say)
- Cards to choose from (Linux or Windows in Add server, and the like) share the dialog's full width
- Add server opened for one kind (a Windows server from Import from Windows, a Linux server's card) offers only that kind, and says which in its title
- The dashed card that adds the first server wraps its text, which ran out of the card
- A server's admin site is told from a Python site, which also runs from its app folder: Servers named a Python site as the admin of a server that had one
- Opening the Server page no longer shows the page's own loading under the part's

## [1.0.69] - 2026-11-24

### Features

- Servers with no other server yet: a dashed card beside this server's adds the first one, in place of a line of text, and Add server comes in the toolbar once there is one
- SQL Server: each database and the latest backup of each have a small icon by their name, and a backup's older ones fold out under it with Older (and their count) in its row, in place of a line under each
- A SQL action (create, back up, delete…) shows its steps, Connect and the work, and when no SQL Server answers or the login is refused, says so in plain words

### Fixes

- The admin's SQL connections to SQL Server here as sa get the new sa password when SQL Server is installed or its sa password changed: they kept their own copy, and SQL Server refused it
- Removing the SQL tools removes the SQL connections too, as none of them works without sqlcmd (the dialog says so)
- Install SQL Server offers to install the SQL tools, and Remove SQL Server to remove them, only when that is not already so

## [1.0.68] - 2026-11-24

### Features

- A scheduled job's log shows acme.sh's certificate runs as cards too, one per run with when it started, and its lines with the time only

### Fixes

- Issue certificate: the button takes the focus once the DNS check has found that the names point here, so Enter issues the certificate (it was disabled while DNS was checked, and lost the focus the dialog gave it)
- The SQL backup's log no longer starts each run with "Ensure SQL Server can read and overwrite backup files" before the run's time, which came out as a card of its own: the job makes the backup folder SQL Server's without saying so, as it always has
- The tests of an environment's binding compare its script without acme.sh's folders, as the other tests of the frozen scripts do (they failed in 1.0.67)

## [1.0.67] - 2026-11-24

### Fixes

- Certificates are renewed: Harbor ran acme.sh without the folders `harbor server init` installed it with, so acme.sh kept each certificate it issued in /root/.acme.sh, where its cron job never looks ("is not an issued domain, skipping"), and none was renewed. Every acme.sh call now names those folders, and the update moves the certificates kept in /root/.acme.sh to where the cron job renews them (`harbor server adopt-certificates`, `--dry-run` tells what it would do). Only certificates for names a site on the server answers to are moved: another server's, left in a server made from a copy of it, stays. One installed that acme.sh knows nowhere is named, to be issued again
- Changing the binding of an HTTPS site whose old binding was its name no longer deletes the new certificate just installed for it, and Issue certificate on a site with HTTPS no longer deletes the certificate its config serves (acme.sh then refused to issue)
- Reissue certificate installs the certificate where the site's config reads it, so each renewal reloads Apache

## [1.0.66] - 2026-11-23

### Features

- Scheduled jobs are listed by name, A to Z (their label, or Harbor's name for its own), whatever order root's crontab has them in

### Fixes

- The output buttons of a scheduled job no longer change on hover when disabled, as no button in the admin does (the test for it failed in 1.0.65)

## [1.0.65] - 2026-11-23

### Features

- Scheduled jobs: the command and where its output goes are one box, with Browse… and two buttons under it: Keep in a log (`>> /var/log/<program>.log 2>&1`, or the log it has already) and Discard (`> /dev/null 2>&1`), and a line saying where the output goes
- Scheduled jobs: a job's log says when cron runs it next, and when it was last run with Run and how it ended

### Fixes

- Scheduled jobs: a job cron has not run says why: not yet since its command was saved (cron's journal knows a changed command only from its next run), or that it should have run at a time that came and went, in place of "Not run in the last 14 days" for a job that runs every day
- Server tools: Backups shows the backup and log shrink jobs by their label and schedule as root's crontab has them, not always every 4 hours
- Scheduled jobs: while a job is edited, the form fills the window with its own Cancel and Save, without Refresh and Close under it
- Scheduled jobs: the job form has room at its top, a long command wraps instead of hiding its end, Browse… no longer looks chosen, and the common schedules have short names that fit on one line

## [1.0.64] - 2026-11-23

### Features

- Scheduled jobs: the form to add or edit a job is laid out anew, with its schedule in words under When and the common schedules as chips of their own

### Fixes

- Scheduled jobs: Esc in the form to add or edit a job goes back to the jobs instead of closing the window

## [1.0.63] - 2026-11-23

### Features

- Scheduled jobs: Run runs a job now, as cron runs it (as root, with cron's few variables and the crontab's own), in the task window, and then shows its log, so a job can be tried before it runs on its own
- Scheduled jobs: a job's log shows each run as a card of its own with the time it started, red where a line looks like an error, when the job starts its output with a line such as `----- 23.11.2026 20:00 -----` (Harbor's backup and log shrink, and scripts made like them)

## [1.0.62] - 2026-11-23

### Features

- The backup and log shrink jobs write a table of what they did (each database with its backup's size, time and speed, or its log before and after), in place of SQL Server's lines for every file. An error is still written as SQL Server gives it, below what was done before it
- Scheduled jobs: a job can have a label of its own, kept as a comment line above it in root's crontab, and shown as the job's title with the schedule under it. The backup and log shrink jobs are named Backup and Log shrink until given another
- Scheduled jobs: Clear log in a job's log empties the file the job writes its output to

### Fixes

- The log shrink no longer says all logs were shrunk when sqlcmd could not be run

## [1.0.61] - 2026-11-23

### Features

- Logs show a request the web server turned away before any site saw it (Apache's AH10244 and AH00126, gunicorn's 'Invalid request'), as bots trying `../../etc/passwd` or `/.env` make, as Blocked and not as an error or a warning, with a filter of its own

## [1.0.60] - 2026-11-20

### Features

- Sync settings to several environments syncs up to 4 at once, each in a lane of its own in the window, in the order the site's page lists them (environment, then binding). Inside Multi action, and within a data sync, still one at a time

## [1.0.59] - 2026-11-20

### Fixes

- Sync setup offers Import at startup only to a site that receives settings or data; for one that receives nothing it is not shown, and its appsettings.json is left as it is

## [1.0.58] - 2026-11-20

### Features

- Sync settings restarts an environment set to Import at startup, so uSync imports the settings it got; others are left running. A data sync still restarts its destination once, after the data
- Sync setup has Import at startup: saving sets uSync's ImportAtStartup in the site's appsettings.json (Settings, or None when off). A site that receives data imports all, as before

## [1.0.57] - 2026-11-20

### Fixes

- Sync settings… chooses no destination when there are several, and its steps name each site: Sync settings to help.kikayo.dk (Production)

### Other

- Test: a skipped environment is named by its address and environment, as the sync's steps name it

## [1.0.56] - 2026-11-20

### Fixes

- Browse… in Scheduled jobs opens the folder of the file the job runs, also when it is quoted ("/etc/acmesh"/acme.sh), instead of failing on a later path such as /dev/null
- A share's name may start with a slash, so [/] can be made and saved
- A share's Name says why it was refused (it starts with a letter or digit) instead of the browser's 'Use the requested format'

## [1.0.55] - 2026-11-20

### Fixes

- The admin lets a share's name have a slash too, by Samba's own rule, so Add share no longer stops at 'Give the share a name and a full folder path'

## [1.0.54] - 2026-11-20

### Fixes

- A Samba share's name may have a slash, as the root share an older server made ([test-01/]), so it can be saved again

## [1.0.53] - 2026-11-20

### Fixes

- An environment that proxies to localhost gets the main site's Rewrite snippets, no longer the warning that it has no ProxyPass /

## [1.0.52] - 2026-11-20

### Fixes

- The packages card's Check stands at the right of its head, as Publish all on the main site's card, and Add is at the left with Remove at the right

## [1.0.51] - 2026-11-20

### Fixes

- A site's publish deletes what its build no longer has, so a removed package's DLL and App_Plugins no longer stop the site
- The VNS.Harbor dashboard manifest carries version 18.3.2.3 too, as the package does

## [1.0.50] - 2026-11-20

### Features

- The VNS.Harbor package links to its GitHub repository, and Pack Nuget puts it in nuget/artifacts (VNS.Harbor 18.3.2.3)

### Fixes

- Remove package scrolls only the list, so its top is no longer cut off under the filter

## [1.0.49] - 2026-11-19

### Features

- Multi action marks the step a site's warning came in, with the warning under it, not a green tick
- The VNS Harbor Sync dashboard follows the backoffice language, Danish or English (VNS.Harbor 18.3.2.1)

### Fixes

- Multi action Publish all publishes the main site of a site without environments, instead of skipping it
- The dashboard's sync lines stay inside the card: Ikke ajour in Danish, the kinds in one column (VNS.Harbor 18.3.2.2)

## [1.0.48] - 2026-11-19

### Features

- Rewrite to domain can be permanent or temporary, and says it always goes to https
- Admin access can answer others with 404 or a redirect, not only 403
- Allowed IPs can close the whole site, and less space around your address
- Your own address added with a click in the Allowed IPs snippet
- General appsettings snippets for sites that are not Umbraco
- Umbraco filter splits into major versions, and what needs attention comes first on a phone

### Fixes

- A site that gets HTTPS later logs to -443-error.log, not to port 80's log
- The Rewrite snippet writes a ProxyPass, as the site's own robots.txt and sitemap.xml are
- Redirect to suggests no / when the whole site is closed
- Allowed IPs suggests only umbraco for an Umbraco site; app_plugins can break Umbraco Forms
- Snippets suggest Umbraco's paths for Umbraco sites only
- Allowed IPs for the whole site with 403 or 404 no longer asks for a redirect address
- Umbraco setting snippets only for an Umbraco site's appsettings.json

### Other

- Style: a snippet without fields has no empty space, and says what it does
- Style: even space between the fields of a snippet
- Style: every snippet choice has a line under it, centred
- Style: snippet choices as segments, not dropdowns
- Style: what others get in admin access as segments, not a half-width select

## [1.0.47] - 2026-11-18

### Fixes

- A new Umbraco 18+ site takes uploads up to 200 MB (Umbraco's own limit is about 28 MB) and resizes images to at most 5000 × 5000. Sites there already are not changed: Edit appsettings.json has both as snippets
- Edit appsettings.json has snippets of Umbraco's settings, as Edit config has its rules: larger uploads (200 MB), image size limits, image cache, image memory pool, image URL signing (HMAC), content version cleanup, unattended upgrades, production runtime mode, stronger passwords, email (SMTP), no trailing slash, the Content Delivery API and less logging. Each is merged into the file where it belongs, after it says what it sets; chosen site by site, saved by hand
- Harbor no longer gives Umbraco 18+ sites an image signing key, and takes off the one 1.0.46 gave them (by the update, and as a site is published): with it, Umbraco answered 400 to an image URL a site makes by hand (media.Url() + "?width=400"), and those images broke. A site that wants the key sets it itself and makes its image URLs with GetCropUrl
- Update SQL Server no longer restarts SQL Server after the package: the package starts it, and a restart while it upgraded its databases hung for up to 30 minutes. It waits until SQL Server lets logins in instead

## [1.0.46] - 2026-11-18

### Fixes

- Umbraco 18+ sites get an image signing key, and sites no longer say what they run in their Server header (1.0.45)

## [1.0.45] - 2026-11-18

### Fixes

- Signing in no longer fails with "Invalid CSRF token" when the sign-in page was left open past the session's idle timeout, or another tab signed in meanwhile. A request from a session that has run out goes to the sign-in page instead of the same error
- In the list of environments on a site's page, a row has its Frontend and Backend; Publish and the ⋯ menu are gone, as the row unfolds to all its buttons
- A phone has Clear logs, Check service, Issue certificate and Change environment on an environment's card
- A phone can add and remove packages on a site's page (Add and Remove beside Check). There the packages are folded: their head unfolds them, with an arrow at its right
- A phone always shows the list of sites and the list of a site's environments: it has no buttons to choose cards. The desktop's choice is kept
- A phone has Publish environments, Sync data… and Sync settings… above the environments, side by side under the title. A phone has no filter of the environments. Delete site and Delete everything are not on a phone
- On a phone, a site's name has a line of its own at the top of its page, with its badges and Publish all below it, as for a long name
- Server tools fit a phone: a tool's name and state on a line, its buttons across the line below; a row's description is below its name and buttons instead of squeezed between them
- Scheduled jobs: Browse… beside the command chooses the file a job runs from the server's folders. The command runs it (by bash or python3 when it may not be run itself) and keeps its output in /var/log/<name>.log
- Making a C# or Umbraco site installs the .NET it needs first when the server does not have it (as .NET 10 for Umbraco 18 after it was removed in Server tools): it stopped with "Umbraco.Templates@18.3.2 is not supported" and a half made site
- Who can reach the admin site, asked when it is installed, can be changed: Who can reach it… on the admin site's page (harbor admin access). Everywhere, or some IP addresses and ranges; it shows your address now, and a list without it is refused unless you tick that it locks you out. Apache checks the change before it is used
- SSH in Server tools: how root may log in over SSH, with a password or a key, a key only, or not at all (harbor server ssh-root). sshd checks the change before it is used, and one it refuses is taken back
- A share's dialog in Server tools: its name and folder side by side, how its files are written as three choices in a row, Read only, Hidden and No login as three cards, and who may log in as chips. A password and its repeat stand side by side
- Samba users in Server tools: Add user… makes a login for the shares alone (its Linux user without a shell or a home), with New password… and Delete on each; each share has Who may log in. The first user besides root closes every share any login could reach to root, so a new user reaches nothing until a share lets it in
- Samba is in Server tools: Share the server… installs Samba, shares the whole server as root and gives root a Samba password. Its shares are listed, with Edit… and Delete on each and Add share… (a folder, read only or not, the user its files are written as, hidden, no login); testparm checks a change before it is used. New password…, and Turn off / Turn on Samba without touching its shares. The address is the server's own name. harbor server init no longer asks about it (harbor server samba)
- harbor server init installs no .NET: the first site that needs it, or Add version… in Server tools, installs it, and with the first one /usr/bin/dotnet, DOTNET_ROOT, dotnet-outdated and the VNS templates
- The newest .NET can be removed when no site uses it (as 10.0 on a server whose sites all run 8.0). The newest host stays, so the other versions keep running. Only the last version stays always
- Server tools say what uses .NET and Node.js: each .NET version and Node.js has Used by N, which unfolds the sites (by their project, what they publish, a package.json or a service). A .NET version a site uses cannot be removed: its button says by whom. Change version… on Node.js shows the sites that use it, and which of them want another version than the chosen one (engines in their package.json)
- Change version… on Node.js goes down a version too (24 to 22): it stopped with "Node.js 22 was not installed", as apt does not go down by itself. Its dialog says the installed version is replaced, and chooses no version for you
- The log shrink leaves a log that is already at its smallest (two virtual log files) alone: it wrote "Cannot shrink log file 2 (…) because total number of logical log files cannot be fewer than 2" for every database every four hours, and switched their recovery model for nothing. It finds a database's log by its type, not by a name ending in _log
- Scheduled jobs: Log on each job shows when cron last ran it (the last 14 days), the end of its output (its log file, or the journal for | logger -t) and the lines that look like errors
- Browse… in Scheduled jobs shows only what a job can run: folders, programs and scripts (sh, py, js, php, pl)
- The Server menu is shorter, in groups without titles: Shell (was Server shell) and Files (/); Tools… (was Server tools…) and Servers…; Apache and Windows; Admin site; Windows (Copy sites…, Import sites…) and Apache (Edit conf, Test config, Error logs, Restart) fold out with their items under them, and are folded each time it opens. It keeps its width as they fold out. Edit conf opens apache2.conf in the editor, and Save tests it before Apache reloads. Admin app log is gone from it: it is Logs on the admin site's page
- Add environment on another Linux server: where the server has a wildcard on one of its domains (a made-up name under it resolves to its public address, as *.kikayo.dk), the suggested folder and binding go under it: umb18.linux-01.vns.dk on kikayo is umb18.kikayo.dk
- Add environment on another Linux server: the suggested folder and binding follow the server chosen where they hold this server's name (umb18.linux-01.vns.dk on linux-02 is umb18.linux-02.vns.dk), what was typed stays; DNS is checked against that server, also with HTTPS off, and HTTPS is a choice only once the binding points there, as in New site
- A dialog that needs another server (Add environment, Copy site, Import and Copy from Windows) shows the servers as cards, the chosen one with a tick, and a dashed Add server card that adds one there and chooses it. Import from Windows reads the sites at once, and again when another server is chosen: Show the sites is gone
- Another server is always one in Servers: Add environment offers a Linux or Windows server only when Servers has one, and Copy site another server likewise; the address and login are no longer typed in those dialogs. Without one, its card stays as a dashed card, Add one in Servers first, which opens Add server for that kind over the dialog; once it is added, the dialog chooses it. The admin site refuses a new environment on a server not in Servers too
- Windows in the Server menu shows only where there is Windows: Copy sites… when a site has an environment on Windows, Import sites… when a Windows server is kept in Servers (Add server, Windows). Import uses that server's share and login, no longer one typed in its dialog
- Server tools comes back where it was scrolled to after an action, and keeps its place when it is drawn again
- While the admin site starts (after an update), a request says so ("The admin site is starting. Try again in a moment.") instead of Service Unavailable
- The browser no longer offers to save the passwords typed in Harbor's dialogs (SSH, Samba, sa, Windows shares), Safari too: the field holds only dots and Harbor keeps what is typed in it, so there is no password in it to save (also not as the page is reloaded); such a dialog's form is run in place, not sent the browser's way, and password managers are told to keep out. A browser that cannot show dots in a text field keeps a password field. The sign-in page is as it was
- Server tools says whether there is an update: SQL Server, the SQL tools and Node.js as apt has them, each .NET version from Microsoft's release list (kept six hours). Update to <version> where there is one, Up to date where there is none, and the plain Update button where it cannot be told
- Servers shows Linux and Windows apart, each under a heading with its count, this server first
- Waiting for a server (Server tools, Servers, Scheduled jobs and a job's log, the sites on a Windows server) shows a turning ring
- New site and Set up site… start with Set up with HTTPS off: it is chosen, not taken for granted. Add environment follows the main site as before. In those dialogs and Add environment, the DNS check stands above the HTTPS choice: whether the name points here first
- Remove server does not uninstall Harbor where it is used: a server with sites of its own (not only environments managed from another server, nor the admin), or one an SQL connection here reaches. The dialog says why, and harbor servers remove refuses
- The update puts every server an environment runs on in Servers where it is not (an environment made by an older release, or before a server had to be chosen from Servers): a Linux one by its own hostname, a Windows one with its environment's share and login. Nothing is done on those servers (harbor servers adopt)
- A server in Servers (Linux or Windows) stays while an environment here runs on it: Remove… shows those environments, each opening its site's page to move or remove it, and Remove server is off until there are none; harbor servers remove refuses too
- Remove server always takes Harbor's key off the server, so this server cannot log in there unseen afterwards: no longer an option. Not while an environment of a site here runs there (it publishes and syncs with the key), nor when it does not answer; the dialog and the task say so
- Publish all (C# sites), Update Python packages, Set up site… and Move under main site are at the right of the main site's card head, as Who can reach it… on the admin site's, no longer beside the site's name
- Update Python packages updates a package pinned with == in requirements.txt too (moved to its latest version there, said first): pip --upgrade kept it where it was, so the update did nothing and the button stayed. Without pip's cache warning
- Move under main site shows the site and its main site side by side, the main sites as cards, and what it means in three points
- Sites no longer say what they run in their Server header (it said Kestrel): Apache takes off the one a site sends, and says only "Apache". An Umbraco 18+ site gets an image signing key of its own (Umbraco:CMS:Imaging:HMACSecretKey, kept for root only), so Umbraco's security check no longer asks for one: in a drop-in of its service, so a publish keeps it, set by the update and when a site is made, set up, published or receives a build. Not restarted by the update: it counts from the next start
- Nothing disabled reacts to the pointer any more: a disabled button, or a card that cannot be chosen (as Content and Media in Sync setup… when the site does not receive), had a hover effect
- The button that opens an environment says Frontend. A site with more environments of the same kind (subsites, each in its own folder) shows a small label with each one's folder on the overview, so they can be told apart
- In the list of sites on a phone, an environment's buttons no longer run off the row: a site looks as its card, each environment on two lines: its lock, badge and folder on the first, its state at the left of the second and its links at the right. On a site's page, a row's lock stands before its name; a narrow screen has the name and the environment's badge on the first line, the state and the links on the second

## [1.0.44] - 2026-11-18

### Fixes

- A multi sync from the overview names who started it in the sync history (and the dashboard in Umbraco), as a sync from an environment's card does: it had no name

## [1.0.43] - 2026-11-18

### Fixes

- A phone has the sync buttons on an environment's card (Sync setup…, Sync data…, Sync settings…), beside Publish, Start, Stop, Restart and Logs
- On a phone, the Deploy row no longer touches the line above the Site row when the rows between them are hidden
- The sync dialog fits a phone: Recent syncs is a short block per sync instead of a wide table, and a destination's name wraps before "Nothing to sync" or the check mark instead of running under it

## [1.0.42] - 2026-11-18

### Changes

- Every action shows all its steps from the start, in the order it takes them: those still to come are greyed, so you see how far it is and what is left. A step it does not get to (it failed before, or a site it was for is gone) is shown as not taken. Steps that only show once it runs (a Python site's packages, say) come in where they belong. Create, setup and publish have new steps of their own: setting up Apache and starting the service, cleaning up, starting the site and checking that it runs
- A new Umbraco site is offered the newest Umbraco 18 on nuget.org (18.3.2 now), not the version Harbor was released with. It is asked in the background and kept an hour; without nuget.org it is the known version. A new major version (19) stays a choice of its own, as the templates and the VNS.Harbor package follow 18
- A new server can be shared over Samba: harbor server init (which the install runs) asks, and when wanted installs Samba and shares / as root under the server's short name (linux-03 on linux-03.vns.dk), for root with a Samba password the install asks for. Without a terminal it says how to set it later (smbpasswd -a root). A share there already, and a password root has, stay as they are

## [1.0.41] - 2026-11-17

### Fixes

- Move out… has a colour and an icon of its own, as the other buttons: it no longer fades into the page
- Backoffice login is offered only on an Umbraco site of 18.3.1 or later, which the VNS.Harbor package needs, or one that has the package already: an older site (such as Umbraco 13) had the button

## [1.0.40] - 2026-11-17

### Fixes

- The publish script reaches the server over SSH by its internal address, as Harbor knows its own addresses (SSH is usually open only inside), not by the public name of its admin; SERVER still gives another. The publish token goes to the admin over HTTPS as before

## [1.0.39] - 2026-11-17

### Fixes

- A publish from a machine leaves what Python writes as it runs alone (__pycache__, *.pyc): the first publish to a Python site stopped on it, as a file the site has and the build does not
- The SSH server and port in the publish script can be given when it runs (SERVER=root@linux-01 ./publish-harbor.sh), for a server whose SSH is not open on its public name. A publish token needs no SSH

## [1.0.38] - 2026-11-17

### Fixes

- The publish script of a Python site finds the folder to send: app when the project has app/app.py, else the project's own folder when app.py is there (it failed with tar: could not chdir to 'app'). SOURCE names another, and without an app.py it says so

## [1.0.37] - 2026-11-17

### Fixes

- The first publish from a machine to a site that has files the publish would delete stops, and names them, until the site has a choice of what it keeps (KEEP=(/wwwroot/), or KEEP=(none) to let them go): a site's own files are not deleted by mistake
- The scripts a publish from a machine sends are kept where only root can write (/var/lib/harbor/sites/<site>/scripts), not in the site's folder, which its app user owns. A script it no longer sends is removed; one written on the server, there or in the site's scripts folder, stays
- The admin runs a site's script only when it and its folder are root's and only root can write them: the app user of a site that runs from its own folder could otherwise swap in a scripts folder of its own and have it run as root. Setup keeps a site's scripts folder root's
- The copy for harbor site rollback is made only when the publish before worked, and a failed publish is kept as failed: after two failures in a row, rollback still goes back to the last one that ran
- A new publish token is saved with a command for the computer chosen: Command Prompt on Windows (in %USERPROFILE%\.config\vns-harbor, where the .cmd script looks), not the macOS/Linux command. The new token stays shown while the dialog is open, also when another computer is chosen

## [1.0.36] - 2026-11-17

### Features

- Publish from your machine without SSH: a publish token of your own, made, shown once and removed in the Publish from your machine dialog (only its fingerprint is kept, root only). With it the script publishes over HTTPS to Harbor (POST /api/publish/<site>), as you and only to the sites you may publish; the output comes back as it runs, and the script fails when the publish does
- The Publish from your machine dialog asks which computer (macOS/Linux or Windows: a .cmd script, with Windows' own tar, curl and ssh) and which access (publish token or SSH as root), and its script, steps and file name follow
- Python and static sites can be published from a machine too: a Python site's app folder (its packages installed when requirements.txt changed, then restarted and checked on gunicorn's port), a static site's files. harbor site rollback works for them as well
- Update Python packages is beside the site's title, as Publish all, and only when pip says a package in requirements.txt has a newer version (how many and which in its tip). What pip says is kept an hour, and asked again after an update

## [1.0.35] - 2026-11-17

### Features

- Publish from your machine sends the build as one tar over one SSH login (no rsync on the machine, so macOS' own is no matter, and no rights from the machine's temporary folder), built for the server's architecture (it asks: linux-x64 or linux-arm64) and from the project named, or the only .csproj in the folder
- Publish from a machine checks the build before anything changes: it must have the dll the site's service starts, and none of what the site keeps on the server (App_Data, umbraco/Data, and its kept paths such as /wwwroot/), which would be a fault in the project
- The paths a site keeps from every publish, such as /wwwroot/ where its files are made on the server (KEEP in the script, harbor site receive-build --keep; kept in local-publish.json from then on)
- appsettings.json can come from the build at every publish (SEND_APPSETTINGS=y in the script, --appsettings build): appsettings.<Environment>.json on the server stays. By default the server's own stays, as before
- A scripts folder beside the publish script goes along: the site's own scripts (.sh) in its scripts folder, owned by root
- After a publish Harbor checks that the site runs and answers on its port (up to a minute); if not, the publish fails, on the machine too, with the site's log
- harbor site rollback <site>: the build before the last publish put back in seconds, the site restarted and checked
- One publish of a site at a time (a lock), and each publish is kept in local-publish.json: when, by whom from which machine, which git commit and whether it had changes not committed

### Fixes

- The scripts folder of a site published from a machine is root's again after a publish: it was given to www-data with the rest of the folder, though the admin runs those scripts as root
- The folder a site runs from keeps its own rights at a publish (such as drwxrwxr-t), not those of the machine's temporary folder
- A later publish no longer changes the binding kept for the site: it is the one the site was made with

## [1.0.34] - 2026-11-17

### Fixes

- Publish from your machine opens on a site set up before 1.0.32 too: it failed with null is not an object (site.local_publish.binding) where the site had not been published from a machine yet

## [1.0.33] - 2026-11-17

### Fixes

- Publish from your machine is offered on every C# site that is only a build here (no project, not an environment of a site elsewhere), also one set up before 1.0.32: its first publish from a machine puts the build in the folder it runs from

## [1.0.32] - 2026-11-17

### Fixes

- New site no longer fails with Could not load installed .NET templates after an update: dotnet is run from / , not from the admin's working folder, which an update can have replaced
- A site with no connection string in its settings shows no Datasource, not Unknown. Unknown is kept for a connection Harbor cannot tell
- New site: From my machine is an option of C# now, Published from an external source. The site is made and runs at once (harbor site create-external): an empty web project with its name is built as the build user, only its build is kept, and the site is set up from it with its service, Apache config and certificate. A project published to it from a machine must build the same dll. The environment comes first in the C# options again, and the local publish folder follows it (_development, _test, _staging, _production) until it is typed over

## [1.0.31] - 2026-11-17

### Features

- From my machine in New site: a C# site with no project on the server, published from your own computer and run as an environment from another server is. Harbor gives you a script for the project's folder: dotnet publish for Linux (framework-dependent), the build to the server with rsync over SSH, and harbor site receive-build there. The first run sets the site up (service, Apache, certificate when chosen); later runs put the new build in place and restart it, never touching its appsettings, database, media or App_Data, and deleting nothing in App_Plugins or uSync. The site's page shows the script again (Publish from your machine)

### Fixes

- A site that is only a build (published from a machine, or put in /var/www by hand) no longer offers a project's scripts (publish, outdated, create_prod, change_env): setup no longer gives them to a build, and the admin leaves them out where they were given before

## [1.0.30] - 2026-11-16

### Fixes

- The admin shows a spinner with Loading sites… while it opens or reloads, not an empty dark page

## [1.0.29] - 2026-11-16

### Fixes

- The Backend button of a site managed from another server opens its backoffice's own login: the one-time link connected the site to the Harbor of the server it runs on (that Harbor's token), and the site's sync then got Harbor cannot be reached

## [1.0.28] - 2026-11-16

### Fixes

- Backoffice login is never offered on a site managed from another server, and saving it there is refused: it connected the site to the Harbor of the server it runs on, which does not know it, so its sync said Harbor cannot be reached. It is set where the site is managed

## [1.0.27] - 2026-11-16

### Fixes

- Multi action runs four sites at a time, not all at once: a sync of 17 sites to one server opened more SSH logins than its sshd lets wait (MaxStartups), and it reset some of them (kex_exchange_identification: Connection reset by peer). Many builds at once also crowded the server. HARBOR_MULTI_PARALLEL sets another number
- The steps of a Multi action are named with their site, and each running site's newest step is shown as active (several sites run at once), failed when its site failed

## [1.0.26] - 2026-11-16

### Fixes

- Multi action opens with nothing looking chosen (the first choice had a ring), and its choices are more compact: four in a row have their icon above the text

## [1.0.25] - 2026-11-16

### Features

- Sync in Multi action: settings, content and media, or everything (the settings first), from each selected main site to the environments its Sync setup lets take them. Multi action is also laid out anew: grouped choices, each with its icon, and a clear chosen state

### Fixes

- Install package on several sites is no longer marked as failed by dotnet's own notes: a package added before its restore (--no-restore), and a build without the MSBuild server ("the server may have failed to start"). Real warnings, such as vulnerable packages, still count

## [1.0.24] - 2026-11-16

### Fixes

- Clear logs, Update packages and Check service no longer hang in their window: systemctl status opened a pager (lines 1-12/12 (END)) and waited for a key. It never does now

## [1.0.23] - 2026-11-16

### Fixes

- An environment on another Linux server or on Windows is headed by its folder on its card (poda.dk), with its binding listed below

## [1.0.22] - 2026-11-16

### Fixes

- A site managed from another server is headed by its folder, as on the server it is managed from (poda.dk, with the binding www.poda.dk). This server's own sites are still headed by their main binding

## [1.0.21] - 2026-11-16

### Features

- Managed elsewhere and Managed here in the overview: the environments of sites on another server, and this server's own sites, each shown alone

### Fixes

- Publishing an environment no longer deletes anything in its App_Plugins (wwwroot/App_Plugins and App_Plugins): new and updated files from the build go there, and what the build does not have stays. Only the main site's own publish may delete there
- Copy from Windows names the copy as its folder on Windows, not by its binding: poda.dk with the binding www.poda.dk is /var/www/poda.dk and the service poda.dk on Linux, as on Windows

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
