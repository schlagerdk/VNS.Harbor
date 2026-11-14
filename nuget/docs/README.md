# VNS Harbor

VNS Harbor for Umbraco 18. Its versions follow Umbraco's: VNS.Harbor 18.3.1 is for Umbraco 18.3.1 and newer, and a fourth number (18.3.1.4) is a fix of the package for the same Umbraco. It links an Umbraco site to VNS Harbor, the server admin that hosts and publishes it.

- **Log in from Harbor:** the Backend button in the Harbor admin opens the site's backoffice already logged in, with no Umbraco password stored or sent.
- **Sync from the backoffice:** the dashboard sends this site's settings and data to its other environments (Development, Test, Staging, Production) through Harbor, and shows each step as it runs.

## Installation

Use the package manager [nuget](https://nuget.org) to install.

```bash
dotnet add package VNS.Harbor
```

A new Umbraco site created in Harbor gets the package already. Nothing else needs to be set up in the site: Harbor writes the connection when it publishes.

## The connection to Harbor

When Harbor publishes the site, it writes `vns-harbor.json` in the folder the site runs from. The file holds:

- the address of the Harbor admin that manages the site
- the site and the environment
- a token for Harbor's API
- a key for backoffice logins

The file is owned by root, and only the site may read it (`root:www-data`, `0640`). It is never placed in `appsettings.json` or in the repository, so it is never synced or committed. The package reads it again when it changes.

Without the file, the package does nothing: the dashboard shows that the site has no connection to Harbor.

## Logging in from Harbor

1. In the Harbor admin, choose which Umbraco user the Backend button logs in as.
2. A click on Backend makes Harbor sign a one-time link to `/umbraco/vns-harbor/login`.
3. The package checks the link and signs that user in with Umbraco's own sign-in. Then it opens `/umbraco`.

A link is accepted only if all of these hold:

- it is signed with the site's login key
- it is for this site and environment
- it is less than 60 seconds old
- it has not been used before

Every other link is refused and sends you to the normal login. Harbor logs each login: who logged in, when, and as which Umbraco user. Only Harbor users with access to the site get the button.

## The dashboard

The **VNS Harbor** dashboard is a tab in the Content section. From there an editor can:

- see the environments this site may sync to
- sync settings to one environment or to all of them. Settings are document types, templates, views, CSS and scripts.
- sync data to one environment or to all of them. Data is merged, so nothing is deleted on the environment.
- sync everything to one environment: settings first, then data
- see on each environment, and in a sync as it runs, which way it goes (such as Production → Staging)
- follow the sync's steps as Harbor runs them

The rules are the same as in the Harbor admin: which environments may send, which may receive, and what each takes part in. The sync itself runs in Harbor, never in the site. The site runs as `www-data` and has no rights to run anything as root.

### Who may use it

Administrators (the Umbraco group **Administrators**) may always sync. The package also creates the user group **VNS Harbor** (alias `vnsHarbor`), empty at first: add the other users who may sync. Everyone else sees no sync buttons, and the API refuses their requests.

### Access to Harbor

The dashboard first checks that the site can reach Harbor's address over HTTPS.

- **The site is on the same server as Harbor:** the connection is local.
- **The site is on another server:** that server must be able to reach the Harbor admin over HTTPS.

If Harbor cannot be reached, the dashboard says so and the sync buttons stay off.

## Requirements

- Umbraco 18.3.1 or newer, .NET 10
- A site published by VNS Harbor, so that `vns-harbor.json` is there

## License

MIT, see `LICENSE.md`.
