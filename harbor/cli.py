"""harbor <group> <command> [arguments]: everything the admin site and the scripts do, as one command.

Each command has the arguments its script had, and reads the same VNS_* settings from the environment, so a script
that has become a wrapper behaves as before.
"""
import argparse
import os
import sys

from harbor.core.system import CommandFailed, Stop, System


def _servername(args, system):
    from harbor.server.apache import set_servername
    set_servername(system)


def _apache_conf(args, system):
    from harbor.server.apache import install_apache_conf
    install_apache_conf(system)


def _tls_policy(args, system):
    from harbor.server.apache import install_tls_policy
    install_tls_policy(system, start_if_stopped=args.start_if_stopped)


def _fix_permissions(args, system):
    from harbor.sites.permissions import fix_permissions
    # The environment, as the script read it: VNS_TARGET, the older single one when it is not set
    fix_permissions(system, args.site, args.scope, os.environ.get("VNS_TARGET") or "prod")


def target():
    """The environment, as the scripts read it: VNS_TARGET, the older single one when it is not set"""
    return os.environ.get("VNS_TARGET") or "prod"


def _site_action(args, system):
    from harbor.sites import actions
    from harbor.core.system import Stop
    if args.action == "issue_certificate":
        actions.issue_certificate(system, args.site, yes=args.yes or args.argument == "--yes")
    elif args.action == "change_env":
        actions.change_environment(system, args.site, args.argument or "")
    else:
        raise Stop(f"Unknown action: {args.action}")


def _environment_action(args, system):
    from harbor.environments import actions
    from harbor.core.system import Stop
    if args.action == "issue_certificate":
        actions.issue_certificate(system, args.site, target(), yes=args.yes or args.argument == "--yes")
    elif args.action == "change_env":
        actions.change_environment(system, args.site, target(), args.argument or "")
    else:
        raise Stop(f"Unknown action: {args.action}")


def _copy_site(args, system):
    from harbor.sites.copy import copy_site
    copy_site(system, args.source, args.target, args.binding, args.database or "", stop_source=args.stop_source, remove_source=args.remove_source or "")


def _setup_site(args, system):
    from harbor.sites.setup import setup_site
    setup_site(system, args.site, args.binding, args.certificate, args.environment)


def _create_external(args, system):
    from harbor.sites.receive_build import create_external
    create_external(system, args.site, args.project, args.binding, args.https, args.environment)


def _receive_build(args, system):
    from harbor.sites.receive_build import receive_build
    receive_build(system, args.site, args.binding, args.https, args.environment, args.keep, args.appsettings,
                  sys.stdin.buffer if args.stdin else None, args.by, args.commit, args.dirty)


def _rollback_build(args, system):
    from harbor.sites.receive_build import rollback
    rollback(system, args.site)


def _delete_site(args, system):
    from harbor.sites.delete import delete_site
    delete_site(system, args.site, args.certificates, args.scope, os.environ.get("VNS_TARGET", ""))


def _change_binding(args, system):
    from harbor.sites.bindings import change_binding
    change_binding(system, args.site, args.binding, args.aliases, yes=args.confirm == "--yes" or args.yes)


def _create_site(args, system):
    from harbor.sites.create import create_site
    create_site(system, args.arguments)


def _register_environment(args, system):
    from harbor.environments.register import register_environment
    register_environment(system, args.site)


def _remote_action(args, system):
    from harbor.environments.remote import remote_action
    remote_action(system, args.site, args.action, target())


def _adopt_site(args, system):
    from harbor.sites.adopt import adopt_site
    adopt_site(system, args.site, args.main)


def _release_site(args, system):
    from harbor.sites.adopt import release_site
    release_site(system, args.main, args.environment)


def _carry_snippets(args, system):
    from harbor.environments.snippets import carry_snippets
    carry_snippets(system, args.site, target())


def _sync_environments(args, system):
    from harbor.environments.sync import sync_environments
    sync_environments(system, args.site, args.source, args.destination, args.data or "", args.dry_run)


def _data_sync(args, system):
    from harbor.environments.data import data_sync
    data_sync(system, args.site, args.action, args.source, args.destinations, args.option[0] if args.option else "")


def _move_environment(args, system):
    from harbor.environments.move import move_environment
    move_environment(system, args.site, args.environment)


def _move_site(args, system):
    from harbor.sites.move import move_site
    move_site(system, args.site)


def _receive_sites(args, system):
    from harbor.sites.move import receive
    receive(system, args.incoming, args.sql_server)


def _sql(args, system):
    from harbor.sql import databases, server
    name = args.command.replace("-", "_")
    module = server if name in ("install_server", "update_server", "uninstall_server", "install_sql_tools", "uninstall_sql_tools",
                                "set_sa_password", "import_sa_password") else databases
    words = {"copy": 2, "drop": 2, "create": 1, "restore": 1}.get(name, 0)
    # The answers the admin gives as options (each is asked in the terminal when it is not given)
    options = {key: getattr(args, key) for key in ("edition", "with_tools", "assume_yes", "delete_data")
               if getattr(args, key, None) not in (None, "")}
    if getattr(args, "older_ubuntu", False):
        options["older_ubuntu"] = True
    getattr(module, name)(system, *getattr(args, "names", [])[:words], **options)


def _postgres(args, system):
    from harbor.sql import postgres
    name = args.command.replace("-", "_")
    if name == "uninstall":
        postgres.uninstall(system, delete_data=args.delete_data, with_tools=args.with_tools)
    else:
        getattr(postgres, name)(system)


def _users_sync(args, system):
    import json
    from harbor.server.users import sync
    try:
        if args.config:
            users = json.loads(system.path(args.config).read_text()).get("users", [])
        else:
            users = json.loads(sys.stdin.read() or "[]")
    except (OSError, ValueError) as error:
        raise Stop(f"The admin's users could not be read: {error}")
    if not isinstance(users, list):
        raise Stop("The admin's users must be a list.")
    sync(system, users)


def _users_sftp(args, system):
    from harbor.server import sftp
    from harbor.server.users import linux_name, mapping
    if args.command == "mounts":
        sftp.remount(system)
        return
    name = linux_name(system, args.user) or (args.user if any(entry.get("linux") == args.user for entry in mapping(system).values()) else "")
    if not name:
        raise Stop(f"No Linux user for {args.user}.")
    if args.command == "sftp-password":
        sftp.set_password(system, name, os.environ.get("HARBOR_SFTP_PASSWORD", ""))
    else:
        sftp.set_keys(system, name, sys.stdin.read())


def _migrate(args, system):
    import json
    from harbor.state import migrate
    if args.rollback:
        migrate.rollback(system, args.site or "")
        return
    if not args.dry_run:
        migrate.migrate_all(system, args.site)
        return
    plans = migrate.dry_run(system, args.site) if not args.json else [migrate.plan(system, name) for name in ([args.site] if args.site else migrate.sites(system))]
    if args.json:
        print(json.dumps(plans, indent=2))
    if not all(item["ready"] for item in plans):
        raise Stop("", 2)


def _windows_move(args, system):
    """Environments on Windows moved to a Linux server Harbor knows (harbor/environments/windows_move.py)"""
    import json
    from harbor.environments import windows_move
    if args.action == "list":
        print(json.dumps(windows_move.candidates(system), indent=2))
    elif args.action == "move":
        if args.site:
            windows_move.move(system, args.site, args.target, args.server, args.alias)
        else:
            # The admin's choice: [{"site", "target", "alias"}], in VNS_WINDOWS_MOVES
            windows_move.move_all(system, json.loads(os.environ.get("VNS_WINDOWS_MOVES") or "[]"), args.server)
    elif args.action == "refresh":
        if args.site:
            windows_move.refresh(system, args.site, args.target, pause=args.pause)
        else:
            windows_move.refresh_all(system, pause=args.pause)
    elif args.action == "take-over":
        windows_move.take_over(system, args.site, args.target)


def _windows(args, system):
    """Sites moved from a Windows server (harbor/sites/windows_import.py); the SMB password in VNS_SMB_PASSWORD"""
    import json
    from harbor.sites import windows_import
    password = os.environ.get("VNS_SMB_PASSWORD", "")
    if getattr(args, "server", ""):
        # A Windows server kept in harbor servers: its share and login
        from harbor.server import servers
        args.share, args.user, password, args.domain = servers.windows_login(system, args.server)
    if args.command in ("list", "import", "import-all") and not (args.share and args.user):
        from harbor.core.system import Stop
        raise Stop("Give the share and --user, or --server with a Windows server kept in harbor servers.")
    if args.command == "list":
        print(json.dumps({"folders": windows_import.list_folders(system, args.share, args.user, password, args.domain)}, indent=2))
    elif args.command == "import":
        windows_import.import_site(system, args.share, args.folder, args.binding, args.user, password, args.domain, args.aliases)
    elif args.command == "import-all":
        # The admin's choice: [{"folder", "binding", "aliases"}], in VNS_WINDOWS_SITES
        windows_import.import_all(system, args.share, json.loads(os.environ.get("VNS_WINDOWS_SITES") or "[]"), args.user, password, args.domain)
    elif args.command == "refresh":
        windows_import.refresh(system, args.site, pause=args.pause)
    elif args.command == "refresh-all":
        windows_import.refresh_all(system, pause=args.pause)
    elif args.command == "take-over":
        windows_import.take_over(system, args.site)


def _servers(args, system):
    import json
    from harbor.server import servers
    if args.command == "add":
        servers.add(system, args.name, args.host, args.user, args.port, os.environ.get("HARBOR_SSH_PASSWORD", ""))
    elif args.command == "remove":
        servers.remove(system, args.name, uninstall=args.uninstall)
    elif args.command == "adopt":
        servers.adopt_environment_servers(system)
    elif args.command == "list":
        print(json.dumps(servers.servers(system), indent=2))
    elif args.command == "add-windows":
        servers.add_windows(system, args.name, args.share, args.user, os.environ.get("VNS_SMB_PASSWORD", ""), args.domain)
    elif args.command == "install":
        servers.install(system, args.name)
    elif args.command == "rollback":
        servers.rollback(system, args.name)
    elif args.command == "set-host":
        servers.set_host(system, args.name, args.host)
    elif args.command == "rename":
        servers.rename(system, args.name, args.new)
    elif args.command == "edit":
        # The name first: the address is then changed under the new one
        name = args.name
        if args.new_name and args.new_name != name:
            servers.rename(system, name, args.new_name)
            name = args.new_name
        if args.host:
            servers.set_host(system, name, args.host)
    else:
        names = [args.name] if args.name else sorted(servers.servers(system))  # a Windows server tells whether its share opens
        # Every server: this one first
        found = ([] if args.name else [servers.here(system)]) + [servers.status(system, name) for name in names]
        print(json.dumps(found, indent=2))


def _users_list(args, system):
    import json
    from harbor.server.users import mapping
    print(json.dumps(mapping(system), indent=2))


def _server_status(args, system):
    import json
    from harbor.server.tools import status
    print(json.dumps(status(system), indent=2))


def _server_cron(args, system):
    from harbor.server.tools import cron, cron_upgrade
    if args.state == "upgrade":
        cron_upgrade(system)
    else:
        cron(system, args.state == "on", args.job)


def _server_dotnet(args, system):
    from harbor.server.tools import dotnet_install, dotnet_remove
    if args.action == "remove":
        dotnet_remove(system, args.channel)
    else:
        dotnet_install(system, args.channel, update=args.action == "update")


def _server_crontab(args, system):
    import json
    from harbor.server.tools import crontab_clear_log, crontab_entries, crontab_log, crontab_run, crontab_save
    if args.action == "run":
        crontab_run(system, args.command)
    elif args.action == "list":
        print(json.dumps(crontab_entries(system), indent=2))
    elif args.action in ("log", "clear-log"):
        try:
            command = json.loads(sys.stdin.read()).get("command", "")
        except (ValueError, AttributeError):
            raise Stop("The job's command comes as JSON on stdin.") from None
        if args.action == "log":
            print(json.dumps(crontab_log(system, command), indent=2))
        else:
            crontab_clear_log(system, command)
    else:
        try:
            entries = json.loads(sys.stdin.read())
        except ValueError:
            raise Stop("The crontab comes as JSON on stdin.") from None
        crontab_save(system, entries if isinstance(entries, list) else [])


def _server_ssh_root(args, system):
    from harbor.server.ssh import set_root_login
    set_root_login(system, args.mode)


def _server_samba(args, system):
    import json
    from harbor.server import samba
    if args.action == "shares":
        print(json.dumps(samba.shares(system), indent=2))
    elif args.action == "save-share":
        samba.save_share(system, {"name": args.name, "path": args.path, "read_only": args.read_only, "browseable": not args.hidden,
                                  "guest_ok": args.guest_ok, "force_user": args.force_user, "valid_users": args.valid_users,
                                  "comment": args.comment}, args.old_name)
    elif args.action == "delete-share":
        samba.delete_share(system, args.name)
    elif args.action in ("start", "stop"):
        samba.turn(system, args.action == "start")
    elif args.action == "users":
        print(json.dumps(samba.users(system), indent=2))
    elif args.action == "add-user":
        samba.add_user(system, args.name)
    elif args.action == "user-password":
        samba.user_password(system, args.name)
    elif args.action == "delete-user":
        samba.delete_user(system, args.name)
    else:
        {"on": samba.share_over_samba, "password": samba.change_password, "off": samba.stop_sharing}[args.action](system)


def _server_node(args, system):
    from harbor.server import tools
    if args.action == "remove":
        tools.node_remove(system)
    else:
        tools.node_install(system, args.major)


def _state_sync(args, system):
    from harbor.state.store import sync_command
    sync_command(system, args.site)


def _site_tool(args, system):
    from harbor.sites import tools
    name = args.command.replace("-", "_")
    if name == "publish":
        tools.publish(system, args.site, args.configuration, args.folder, args.environment_name)
    elif name == "restart_apache":
        tools.restart_apache(system)
    else:
        getattr(tools, name)(system, args.site)


def _clear_environment_logs(args, system):
    from harbor.sites.tools import clear_environment_logs
    clear_environment_logs(system, args.site, target())


def _publish_environment(args, system):
    from harbor.environments.publish import publish_environment
    publish_environment(system, args.site, target(), args.project, args.local_publish, dry_run=args.dry_run)


def _create_environment(args, system):
    from harbor.core.templates import Site
    from harbor.environments import settings
    from harbor.environments.create import Main, create_environment
    from harbor.sites.actions import site_named
    site = site_named(system, args.site)
    if args.site_root:
        main = Main(site.name, args.site_root, args.binding, args.project, args.publish_folder, args.admin_mail, args.kind)
    else:
        # Not rendered for it: the site's own values, as the admin renders them
        found = settings.load_main_site(system, site)
        main = Main(site.name, site.site_root, found.binding, found.project_name, found.publish_folder, found.admin_mail, found.csharp_kind)
    create_environment(system, main, args.target)


def _connect(args, system):
    from harbor.umbraco import backoffice
    backoffice.connect(system, args.site, args.environment, args.harbor_url)


def _backoffice_user(args, system):
    from harbor.umbraco import backoffice
    backoffice.set_backoffice_user(system, args.site, args.user, args.environment)


def _backoffice_link(args, system):
    from harbor.umbraco import backoffice
    system.say(backoffice.login_link(system, args.site, args.environment, args.who))


def _server_init(args, system):
    from harbor.server.init import init
    init(system)


def _adopt_certificates(args, system):
    from harbor.server.acme import adopt
    adopt(system, args.dry_run)


def _admin(args, system):
    from harbor.install import admin
    if args.command == "install":
        admin.install(system, args.site, args.binding, args.user, args.https, args.allow)
    elif args.command == "update":
        admin.update(system, args.site)
    elif args.command == "set-password":
        admin.set_password(system, args.site)
    elif args.command == "uninstall":
        admin.uninstall(system, args.site, args.delete_certificate)
    elif args.command == "access":
        if args.everyone or args.allow:
            admin.set_allowed_addresses(system, args.site, [] if args.everyone else args.allow, args.others, args.redirect_to)
        else:
            import json
            print(json.dumps(admin.access_settings(system, args.site)))


def _self(args, system):
    from harbor.install import release
    if args.command == "uninstall":
        from harbor.install.uninstall import uninstall
        uninstall(system, assume_yes=args.yes)
    elif args.command == "after-update":
        from harbor.install import update
        update.after_update(system)
    elif args.command == "install":
        release.install(system, args.source)
    elif args.command == "rollback":
        release.rollback(system)
    elif args.command == "use":
        release.use(system, args.release)
    elif args.command == "update":
        from harbor.install import update
        update.update(system, args.ref)
    elif args.command == "finish-update":
        from harbor.install import update
        update.finish_update(system, args.source)
    elif args.command == "check":
        import json
        from harbor.install import update
        print(json.dumps(update.check(system), indent=2))
    elif args.command == "releases":
        for item in release.releases(system):
            system.say(f"{'*' if item['current'] else ' '} {item['name']}  {item['installed']}")
    else:
        release.version(system)


def _environment_binding(args, system):
    from harbor.environments.bindings import change_binding
    change_binding(system, args.site, args.binding, args.aliases, yes=args.confirm == "--yes" or args.yes, target=target())


def _windows_file(args, system):
    from harbor.environments.actions import windows_file
    windows_file(system, args.site, args.action, args.file, target())


def _windows_files(args, system):
    from harbor.environments.actions import windows_files
    windows_files(system, args.site, target())


def parser():
    root = argparse.ArgumentParser(prog="harbor", description="VNS Harbor: web sites on this server, and their environments.")
    groups = root.add_subparsers(dest="group", required=True, metavar="<group>")

    server = groups.add_parser("server", help="the server itself").add_subparsers(dest="command", required=True, metavar="<command>")
    command = server.add_parser("restart-apache", help="restart Apache (was a site's restart_apache.sh)")
    command.set_defaults(handler=_site_tool)
    command = server.add_parser("set-servername", help="give Apache a global ServerName (was set_servername.sh)")
    command.set_defaults(handler=_servername)
    command = server.add_parser("install-apache-conf", help="the server-wide Apache configs (was install_apache_conf.sh)")
    command.set_defaults(handler=_apache_conf)
    command = server.add_parser("install-tls-policy", help="the shared TLS policy (was install_tls_policy.sh)")
    command.add_argument("--start-if-stopped", action="store_true")
    command.set_defaults(handler=_tls_policy)
    command = server.add_parser("init", help="make a new server ready for sites: Apache, acme.sh, keep-alive (was apache/init.sh). .NET comes with the first site that needs it")
    command.set_defaults(handler=_server_init)
    command = server.add_parser("adopt-certificates", help="move the certificates acme.sh kept in /root/.acme.sh to where its cron job renews them (1.0.67)")
    command.add_argument("--dry-run", action="store_true", help="tell what it would do, change nothing")
    command.set_defaults(handler=_adopt_certificates)
    command = server.add_parser("status", help="SQL Server, sqlcmd, the sa password, cron jobs, .NET and Node.js here, as JSON")
    command.set_defaults(handler=_server_status)
    command = server.add_parser("cron", help="the backup and log shrink jobs in root's crontab: on, off, or upgrade (older jobs to harbor commands)")
    command.add_argument("state", choices=("on", "off", "upgrade"))
    command.add_argument("--job", choices=("backup_everything", "shrink_logs", "postgres_backup"), help="on: only this job (as Scheduled jobs suggests it)")
    command.set_defaults(handler=_server_cron)
    command = server.add_parser("dotnet", help="install a .NET channel (SDK and ASP.NET Core runtime), update it to its newest patch, or remove it")
    command.add_argument("action", choices=("install", "update", "remove"))
    command.add_argument("channel", help="such as 10.0")
    command.set_defaults(handler=_server_dotnet)
    command = server.add_parser("crontab", help="root's crontab: its lines as JSON (list), saved from JSON on stdin (save), a job's runs and output (log), its output file emptied (clear-log), or a job run now (run --command)")
    command.add_argument("action", choices=("list", "save", "log", "clear-log", "run"))
    command.add_argument("--command", default="", help="run: the job's command, as it is in root's crontab")
    command.set_defaults(handler=_server_crontab)
    command = server.add_parser("ssh-root", help="how root may log in over SSH: yes (a password or a key), prohibit-password (a key only) or no")
    command.add_argument("mode", choices=("yes", "prohibit-password", "no"))
    command.set_defaults(handler=_server_ssh_root)
    command = server.add_parser("samba", help="share the whole server over Samba as root (on), give root a new Samba password (password), "
                                              "or stop sharing it (off); the password from HARBOR_SAMBA_PASSWORD, else asked")
    command.add_argument("action", choices=("on", "password", "off", "shares", "save-share", "delete-share", "start", "stop",
                                            "users", "add-user", "user-password", "delete-user"),
                         help="on/off: the whole server shared or not; shares: as JSON; save-share: from JSON on stdin; start/stop: Samba itself")
    command.add_argument("name", nargs="?", default="", help="the share (save-share, delete-share) or the user (add-user, user-password, delete-user)")
    command.add_argument("--path", default="", help="save-share: the folder it shares")
    command.add_argument("--old-name", default="", help="save-share: the share it was, when it is changed")
    command.add_argument("--read-only", action="store_true", help="save-share: nothing can be changed through it")
    command.add_argument("--hidden", action="store_true", help="save-share: not listed when the server is browsed")
    command.add_argument("--guest-ok", action="store_true", help="save-share: reached without a login")
    command.add_argument("--force-user", default="", help="save-share: the user its files are read and written as, such as root or www-data")
    command.add_argument("--valid-users", default="", help="save-share: who may log in to it, such as root")
    command.add_argument("--comment", default="", help="save-share: what it is")
    command.set_defaults(handler=_server_samba)
    command = server.add_parser("node", help="install Node.js of a major version (NodeSource), or remove it")
    command.add_argument("action", choices=("install", "remove"))
    command.add_argument("major", nargs="?", default="", help="such as 22")
    command.set_defaults(handler=_server_node)

    site = groups.add_parser("site", help="a site on this server").add_subparsers(dest="command", required=True, metavar="<command>")
    command = site.add_parser("fix-permissions", help="the folder the site runs from owned by www-data (was fix_permissions.sh)")
    command.add_argument("site")
    command.add_argument("scope", nargs="?", default="test", help="test (the site here) or prod (the environment in VNS_TARGET)")
    command.set_defaults(handler=_fix_permissions)
    command = site.add_parser("action", help="issue_certificate or change_env on the site here (was test_action.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("action", nargs="?", default="")
    command.add_argument("argument", nargs="?", default="", help="the environment for change_env, or --yes")
    command.add_argument("--yes", action="store_true", help="no question before a certificate")
    command.set_defaults(handler=_site_action)
    command = site.add_parser("copy", help="copy a site to a new folder here (was copy_site.sh)")
    command.add_argument("source")
    command.add_argument("target")
    command.add_argument("binding")
    command.add_argument("database", nargs="?", default="")
    command.add_argument("--stop-source", action="store_true", help="stop the source while it is copied, then start it again")
    command.add_argument("--remove-source", choices=("config", "all"), help="move: remove the source's service and Apache config, or all of it, once the copy runs")
    command.set_defaults(handler=_copy_site)
    command = site.add_parser("setup", help="set up a folder put in /var/www by hand (was setup_site.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("binding", nargs="?", default="", help="the binding, or * to answer any name")
    command.add_argument("certificate", nargs="?", default="", help="y or n")
    command.add_argument("environment", nargs="?", default="")
    command.set_defaults(handler=_setup_site)
    command = site.add_parser("create-external", help="a C# site published from an external source: made now from an empty "
                              "web project's build (the project is not kept), to be replaced by builds of PROJECT.dll")
    command.add_argument("site")
    command.add_argument("--project", required=True, help="the dll the site runs, as the published project names it")
    command.add_argument("--binding", default="", help="the binding, or * to answer any name")
    command.add_argument("--https", default="n", choices=("y", "n"))
    command.add_argument("--environment", default="Production", choices=("Development", "Test", "Staging", "Production"))
    command.set_defaults(handler=_create_external)
    command = site.add_parser("receive-build", help="a C# site published from a developer's machine: the build it sent to "
                              "/var/lib/harbor/incoming/<site> set up (the first time) or put in place, and the site restarted")
    command.add_argument("site")
    command.add_argument("--binding", default="", help="the first time: the binding, or * to answer any name")
    command.add_argument("--https", default="n", choices=("y", "n"), help="the first time: a certificate")
    command.add_argument("--environment", default="Production", choices=("Development", "Test", "Staging", "Production"))
    command.add_argument("--keep", action="append", default=None, metavar="PATH",
                         help="a path the site keeps from its publishes, such as /wwwroot/ (again for more); kept from then on")
    command.add_argument("--appsettings", choices=("build", "server"), default=None,
                         help="build: appsettings.json from the build at every publish (appsettings.<Environment>.json stays); "
                              "server: only the first time. Kept from then on")
    command.add_argument("--stdin", action="store_true", help="the build as a tar on stdin (build/ and scripts/), as the script sends it")
    command.add_argument("--by", default="", help="who published, and from which machine")
    command.add_argument("--commit", default="", help="the git commit published")
    command.add_argument("--dirty", default="", choices=("", "y", "n"), help="y: with changes not committed")
    command.set_defaults(handler=_receive_build)
    command = site.add_parser("rollback", help="a site published from a machine: the build before its last publish put back")
    command.add_argument("site")
    command.set_defaults(handler=_rollback_build)
    command = site.add_parser("delete", help="delete a site, its environments or one of them (was delete_site.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("certificates", nargs="?", default="", help="y to revoke and remove the certificates")
    command.add_argument("scope", nargs="?", default="all", help="all, test (the site itself) or prod (the environment in VNS_TARGET)")
    command.set_defaults(handler=_delete_site)
    command = site.add_parser("create", help="create an HTML, Python or C# site (was create_site.sh)")
    command.add_argument("arguments", nargs="*", help="folder, html|python|csharp, umbraco|empty, template version, project, binding, "
                         "publish folder, Debug|Release, y|n certificate, template, environment, template package; asked for when not given")
    command.set_defaults(handler=_create_site)
    command = site.add_parser("adopt", help="make a published site an environment of its main site (was adopt_site.sh)")
    command.add_argument("site")
    command.add_argument("main")
    command.set_defaults(handler=_adopt_site)
    command = site.add_parser("release", help="make an environment a separate site again (was adopt_site.sh --release)")
    command.add_argument("main")
    command.add_argument("environment", nargs="?", default="")
    command.set_defaults(handler=_release_site)
    command = site.add_parser("move", help="migrate or copy a site with its environments here to another server, "
                              "the server in VNS_MOVE_* (was move_site.sh)")
    command.add_argument("site")
    command.set_defaults(handler=_move_site)
    command = site.add_parser("receive", help="on the new server: set up the sites that arrived in a folder (was move_site.sh --receive)")
    command.add_argument("incoming")
    command.add_argument("sql_server", nargs="?", default="")
    command.set_defaults(handler=_receive_sites)
    # The tools a site's scripts folder had (the site templates of VNS 1.x)
    for name, text in (("check-service", "the service's status (was check_service.sh)"),
                       ("restart-service", "restart the service and see that it runs (was restart_service.sh)"),
                       ("edit-conf", "edit the Apache config, then test and reload (was edit_conf.sh)"),
                       ("edit-service", "edit the service, then reload and restart (was edit_service.sh)"),
                       ("clear-logs", "Umbraco's logs, with the site stopped (was clear_logs.sh)"),
                       ("update-packages", "a Python site's packages upgraded (was update_packages.sh)"),
                       ("fix-app-permissions", "a Python site's app folder owned by www-data (was fix_permissions.sh)"),
                       ("reissue-certificate", "the certificate issued again for the config as it is (was issue_certificat.sh)"),
                       ("outdated", "the project's outdated packages, and an upgrade if wanted (was outdated.sh)"),
                       ("publish", "build into the publish folder and start again (was publish.sh / publish_local.sh)")):
        command = site.add_parser(name, help=text)
        command.add_argument("site")
        if name == "publish":
            command.add_argument("configuration", nargs="?", default="", help="Debug or Release")
            command.add_argument("--folder", default="", help="the publish folder, when not the one the service runs from")
            command.add_argument("--environment-name", default="", help="-p:EnvironmentName for the build, as a very old script had")
        command.set_defaults(handler=_site_tool)
    # VNS.Harbor in Umbraco (phase 9)
    command = site.add_parser("connect", help="write vns-harbor.json where the site runs, for the VNS.Harbor package")
    command.add_argument("site")
    command.add_argument("--environment", default="main", help="main, or an environment on this server")
    command.add_argument("--harbor-url", default="", help="Harbor's address, as the site reaches it")
    command.set_defaults(handler=_connect)
    command = site.add_parser("backoffice-user", help="the Umbraco user the Backend button logs in as (empty: the login)")
    command.add_argument("site")
    command.add_argument("user", nargs="?", default="")
    command.add_argument("--environment", default="main")
    command.set_defaults(handler=_backoffice_user)
    command = site.add_parser("backoffice-link", help="a one-time backoffice login link (VNS.Harbor package)")
    command.add_argument("site")
    command.add_argument("--environment", default="main")
    command.add_argument("--who", default="", help="who asked for it, for the log")
    command.set_defaults(handler=_backoffice_link)
    command = site.add_parser("binding", help="change the binding and aliases of the site here (was change_binding.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("binding", nargs="?", default="", help="the new binding, or * to answer any name")
    command.add_argument("aliases", nargs="?", default=None, help="the aliases, separated by commas or spaces; asked for when not given")
    command.add_argument("confirm", nargs="?", default="", help="--yes")
    command.add_argument("--yes", action="store_true")
    command.set_defaults(handler=_change_binding)

    sql = groups.add_parser("sql", help="the local SQL Server and its databases").add_subparsers(dest="command", required=True, metavar="<command>")
    for name, words, text in (
            ("create", 1, "a database with a login of its own (was create_db.sh)"),
            ("copy", 2, "a database copied to a new one with a login of its own (was copy_db.sh)"),
            ("restore", 1, "a database from its backup, with a login of its own (was restore_db.sh)"),
            ("drop", 2, "a database, and its backup if wanted (was drop_db.sh)"),
            ("list-databases", 0, "the databases (was list_dbs.sh)"),
            ("list-backups", 0, "the backup files (was list_backups.sh)"),
            ("backup-everything", 0, "every database backed up (was cron/backup_everything.sh)"),
            ("shrink-logs", 0, "every log shrunk (was cron/shrink_logs.sh)"),
            ("add-to-crontab", 0, "the log shrink and backup in root's crontab (was cron/add_to_crontab.sh)"),
            ("install-server", 0, "SQL Server, the sa password in the scripts and sqlcmd (was install_server.sh)"),
            ("update-server", 0, "SQL Server and sqlcmd to their latest versions (was update_server.sh)"),
            ("uninstall-server", 0, "SQL Server removed, and sqlcmd if wanted (was uninstall_server.sh)"),
            ("install-sql-tools", 0, "sqlcmd without SQL Server (was install_tools.sh)"),
            ("uninstall-sql-tools", 0, "sqlcmd removed (was uninstall_tools.sh)"),
            ("set-sa-password", 0, "the sa password in the scripts in sql/ (was set_sa_password.sh)"),
            ("import-sa-password", 0, "the sa password from the scripts in sql/ into Harbor's secrets, once")):
        command = sql.add_parser(name, help=text)
        if name in ("install-server", "update-server", "uninstall-server", "uninstall-sql-tools", "set-sa-password"):
            command.add_argument("--yes", dest="assume_yes", action="store_const", const=True, default=None,
                                 help="answer yes instead of asking")
        if name == "install-server":
            command.add_argument("--edition", default="", help="Developer, Express, Evaluation, Standard or Enterprise: set up "
                                 "without questions, the sa password from HARBOR_SA_PASSWORD")
            command.add_argument("--with-tools", dest="with_tools", action="store_const", const=True, default=None)
            command.add_argument("--without-tools", dest="with_tools", action="store_const", const=False)
            command.add_argument("--older-ubuntu", action="store_true", help="where Microsoft has no SQL Server for this Ubuntu: "
                                 "install the one for an older Ubuntu (not supported by Microsoft)")
        if name == "uninstall-server":
            command.add_argument("--with-tools", dest="with_tools", action="store_const", const=True, default=None, help="remove sqlcmd too")
            command.add_argument("--without-tools", dest="with_tools", action="store_const", const=False, help="keep sqlcmd")
            command.add_argument("--delete-data", dest="delete_data", action="store_const", const=True, default=None)
            command.add_argument("--keep-data", dest="delete_data", action="store_const", const=False)
        if words:
            # As the script: what is not given is asked for, and more is not read
            command.add_argument("names", nargs="*", default=[])
        command.set_defaults(handler=_sql)

    postgres = groups.add_parser("postgres", help="PostgreSQL here, and its tools").add_subparsers(dest="command", required=True, metavar="<command>")
    for name, text in (
            ("install", "PostgreSQL from Ubuntu's packages, its postgres password from HARBOR_POSTGRES_PASSWORD, a connection in the admin"),
            ("update", "PostgreSQL and its tools to the newest packages"),
            ("uninstall", "PostgreSQL removed; its databases kept unless --delete-data"),
            ("install-tools", "psql, pg_dump and pg_restore, to reach a PostgreSQL elsewhere"),
            ("uninstall-tools", "the PostgreSQL tools removed"),
            ("set-password", "the postgres password from HARBOR_POSTGRES_PASSWORD"),
            ("backup-everything", "every database dumped into the backup folder")):
        command = postgres.add_parser(name, help=text)
        if name == "uninstall":
            command.add_argument("--delete-data", action="store_true")
            command.add_argument("--with-tools", action="store_true")
        command.set_defaults(handler=_postgres)

    command = groups.add_parser("migrate", help="Harbor's copy as the truth, the generated scripts out of the site folders (phase 6)")
    command.add_argument("site", nargs="?", default="", help="one site; every site when left out")
    command.add_argument("--dry-run", action="store_true", help="tell what it would do, change nothing")
    command.add_argument("--json", action="store_true", help="the plan as JSON")
    command.add_argument("--rollback", action="store_true", help="put a migrated site's scripts back from its backup")
    command.set_defaults(handler=_migrate, command="")
    windows = groups.add_parser("windows", help="sites moved from a Windows server (IIS) while it stays live").add_subparsers(
        dest="command", required=True, metavar="<command>")
    command = windows.add_parser("list", help="the folders in a share, and what each holds, as JSON (password in VNS_SMB_PASSWORD)")
    command.add_argument("share", nargs="?", default="", help="//server/share/folder")
    command.add_argument("--user", default="")
    command.add_argument("--server", default="", help="a Windows server kept in harbor servers, instead of the share and login")
    command.add_argument("--domain", default="")
    command.set_defaults(handler=_windows)
    command = windows.add_parser("import", help="a folder with a project, or static files, set up here on HTTP")
    command.add_argument("share", help="//server/share/folder, or the name of a Windows server with --server")
    command.add_argument("folder")
    command.add_argument("binding")
    command.add_argument("--user", default="")
    command.add_argument("--server", default="", help="a Windows server kept in harbor servers, instead of the share and login")
    command.add_argument("--domain", default="")
    command.add_argument("--aliases", default="", help="temporary names to test it by, separated by commas")
    command.set_defaults(handler=_windows)
    command = windows.add_parser("import-all", help="the folders in VNS_WINDOWS_SITES (JSON: folder, binding, aliases), one at a time")
    command.add_argument("share", nargs="?", default="")
    command.add_argument("--user", default="")
    command.add_argument("--server", default="", help="a Windows server kept in harbor servers, instead of the share and login")
    command.add_argument("--domain", default="")
    command.set_defaults(handler=_windows)
    command = windows.add_parser("refresh-all", help="every site imported from Windows, refreshed one at a time")
    command.add_argument("--pause", action="store_true")
    command.set_defaults(handler=_windows)
    command = windows.add_parser("refresh", help="the live site's data fetched again (SQLite, media, uSync; all files of an HTML site)")
    command.add_argument("site")
    command.add_argument("--pause", action="store_true", help="take the site on Windows offline while its database is copied")
    command.set_defaults(handler=_windows)
    command = windows.add_parser("take-over", help="after the address moved: the aliases go, a certificate, and the Windows source is forgotten")
    command.add_argument("site")
    command.set_defaults(handler=_windows)
    other = groups.add_parser("servers", help="other servers Harbor works with, reached with its own SSH key").add_subparsers(
        dest="command", required=True, metavar="<command>")
    command = other.add_parser("add", help="add a server: Harbor's key is put there with the password in HARBOR_SSH_PASSWORD, once")
    command.add_argument("name", help="a name of its own, such as linux-02")
    command.add_argument("host", help="its address")
    command.add_argument("--user", default="root")
    command.add_argument("--port", default="22")
    command.set_defaults(handler=_servers)
    command = other.add_parser("adopt", help="put the servers the environments here run on in Servers, where they are not (run by the update)")
    command.set_defaults(handler=_servers)
    command = other.add_parser("remove", help="forget a server here and take Harbor's key off it (not while environments here run there); Harbor stays there unless --uninstall")
    command.add_argument("name")
    command.add_argument("--uninstall", action="store_true", help="also uninstall Harbor there (/opt/harbor and its command only)")
    command.set_defaults(handler=_servers)
    command = other.add_parser("install", help="install this server's Harbor release there and use it (an agent); again updates it")
    command.add_argument("name")
    command.set_defaults(handler=_servers)
    command = other.add_parser("add-windows", help="a Windows server's share and login, kept for Import from Windows (password in VNS_SMB_PASSWORD)")
    command.add_argument("name", help="a name of its own, such as windows-01")
    command.add_argument("share", help="//server/share/folder")
    command.add_argument("--user", required=True)
    command.add_argument("--domain", default="")
    command.set_defaults(handler=_servers)
    command = other.add_parser("set-host", help="the server has a new address: kept here, and in every environment that was on the old one")
    command.add_argument("name")
    command.add_argument("host", help="its new address")
    command.set_defaults(handler=_servers)
    command = other.add_parser("rename", help="a server's name in Servers changed; nothing changes on the server")
    command.add_argument("name")
    command.add_argument("new")
    command.set_defaults(handler=_servers)
    command = other.add_parser("edit", help="a server's name and address at once (rename, then set-host)")
    command.add_argument("name")
    command.add_argument("--name", dest="new_name", default="")
    command.add_argument("--host", default="")
    command.set_defaults(handler=_servers)
    command = other.add_parser("rollback", help="the release installed there before the one in use, used again there")
    command.add_argument("name")
    command.set_defaults(handler=_servers)
    command = other.add_parser("list", help="the servers, as JSON")
    command.set_defaults(handler=_servers)
    command = other.add_parser("status", help="the version handshake with a server, or every server, as JSON")
    command.add_argument("name", nargs="?", default="")
    command.set_defaults(handler=_servers)

    users = groups.add_parser("users", help="a Linux user for each user of the admin").add_subparsers(dest="command", required=True, metavar="<command>")
    command = users.add_parser("sync", help="the Linux users equal to the admin's users (JSON on stdin, or --config)")
    command.add_argument("--config", default="", help="the admin's config.json")
    command.set_defaults(handler=_users_sync)
    command = users.add_parser("list", help="which admin user has which Linux user, as JSON")
    command.set_defaults(handler=_users_list)
    command = users.add_parser("sftp-password", help="set a user's SFTP password (from HARBOR_SFTP_PASSWORD)")
    command.add_argument("user", help="the admin user's id, or their Linux name")
    command.set_defaults(handler=_users_sftp)
    command = users.add_parser("sftp-keys", help="set a user's public SSH keys for SFTP (one per line on stdin; none removes them)")
    command.add_argument("user", help="the admin user's id, or their Linux name")
    command.set_defaults(handler=_users_sftp)
    command = users.add_parser("mounts", help="the sites in the SFTP folders again, as after the last sync (at boot)")
    command.set_defaults(handler=_users_sftp)

    admin = groups.add_parser("admin", help="the admin site, run from the Harbor release in use").add_subparsers(dest="command", required=True, metavar="<command>")
    command = admin.add_parser("install", help="add the admin site (the password from VNS_ADMIN_PASSWORD, or asked for) (was install_admin_site.sh)")
    command.add_argument("site", nargs="?", default="", help="its folder in /var/www")
    command.add_argument("binding", nargs="?", default="")
    command.add_argument("user", nargs="?", default="", help="the first user's name")
    command.add_argument("https", nargs="?", default="", help="y or n")
    command.add_argument("--allow", default=None, help="the IP addresses or ranges allowed in, separated by spaces (empty: everyone)")
    command.set_defaults(handler=_admin)
    command = admin.add_parser("update", help="run the admin site from the release in use, with its Python and libraries up to date")
    command.add_argument("site", nargs="?", default="")
    command.set_defaults(handler=_admin)
    command = admin.add_parser("set-password", help="a new password for the admin site's first login; signs everyone out")
    command.add_argument("site", nargs="?", default="")
    command.set_defaults(handler=_admin)
    command = admin.add_parser("access", help="who may reach the admin site: as JSON, or set (--allow ADDRESS ..., or --everyone)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("--allow", nargs="+", default=[], help="IP addresses or CIDR ranges")
    command.add_argument("--everyone", action="store_true", help="no restriction")
    command.add_argument("--others", choices=("forbidden", "notfound", "redirect"), default="forbidden",
                         help="what other addresses get: 403 (default), 404, or a redirect (--redirect-to)")
    command.add_argument("--redirect-to", default="", metavar="URL", help="where --others redirect sends them")
    command.set_defaults(handler=_admin)
    command = admin.add_parser("uninstall", help="remove the admin site: files, login, service and Apache config")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("delete_certificate", nargs="?", default="", help="y to revoke and remove its certificate")
    command.set_defaults(handler=_admin)

    own = groups.add_parser("self", help="Harbor itself: its releases in /opt/harbor").add_subparsers(dest="command", required=True, metavar="<command>")
    command = own.add_parser("update", help="update to the newest release on GitHub (or --ref vX.Y.Z); the release installs itself")
    command.add_argument("--ref", default="")
    command.set_defaults(handler=_self)
    command = own.add_parser("finish-update", help="install this copy of a release (run by self update from the copy it fetched)")
    command.add_argument("source")
    command.set_defaults(handler=_self)
    command = own.add_parser("uninstall", help="take Harbor off this server; the sites go on running (its state and secrets kept in an archive)")
    command.add_argument("--yes", action="store_true", help="without typing the server's name to confirm")
    command.set_defaults(handler=_self)
    command = own.add_parser("after-update", help="bring the server up to the release in use (run by finish-update)")
    command.set_defaults(handler=_self)
    command = own.add_parser("check", help="the installed version, and the releases on GitHub that are newer, as JSON")
    command.set_defaults(handler=_self)
    command = own.add_parser("use", help="use a release that is in /opt/harbor/releases (one copied here from another server)")
    command.add_argument("release")
    command.set_defaults(handler=_self)
    command = own.add_parser("install", help="install a release from a copy of the repository and use it")
    command.add_argument("source")
    command.set_defaults(handler=_self)
    for name, text in (("version", "the release in use"), ("releases", "the installed releases"), ("rollback", "use the release before the one in use again")):
        own.add_parser(name, help=text).set_defaults(handler=_self)

    state = groups.add_parser("state", help="Harbor's own copy of the state in the site folders").add_subparsers(dest="command", required=True, metavar="<command>")
    command = state.add_parser("sync", help="make Harbor's copy equal to the site folders, of one site or all")
    command.add_argument("site", nargs="?", default="")
    command.set_defaults(handler=_state_sync)

    environment = groups.add_parser("env", help="an environment of a site, chosen with VNS_TARGET").add_subparsers(dest="command", required=True, metavar="<command>")
    command = environment.add_parser("windows-move", help="Copy from Windows: environments on Windows copied to a Linux server Harbor knows, while Windows runs them")
    command.add_argument("action", choices=("list", "move", "refresh", "take-over"))
    command.add_argument("site", nargs="?", default="", help="move all in VNS_WINDOWS_MOVES, or refresh all, without one")
    command.add_argument("target", nargs="?", default="", help="move: the environment on Windows; refresh and take-over: the one it became")
    command.add_argument("--server", default="", help="move: the Linux server, as named in harbor servers")
    command.add_argument("--alias", default="", help="move: a temporary name to test it by")
    command.add_argument("--pause", action="store_true", help="refresh: take the site on Windows offline while its database is copied")
    command.set_defaults(handler=_windows_move)
    command = environment.add_parser("action", help="issue_certificate or change_env where the environment is (was prod_action.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("action", nargs="?", default="")
    command.add_argument("argument", nargs="?", default="", help="the environment for change_env, or --yes")
    command.add_argument("--yes", action="store_true", help="no question before a certificate")
    command.set_defaults(handler=_environment_action)
    command = environment.add_parser("register", help="make a site on another Linux server an environment of a site here, "
                                     "with VNS_PROD_IP, _USER, _PASSWORD, _SSH_PORT and _FOLDER (was register_env.sh)")
    command.add_argument("site")
    command.set_defaults(handler=_register_environment)
    command = environment.add_parser("remote", help="status, start, stop, restart, logs, check_service, edit_service, edit_conf, "
                                     "shell, files, config, scripts or run_script (VNS_SCRIPT) of an environment on another Linux server (was remote_site.sh)")
    command.add_argument("site")
    command.add_argument("action", nargs="?", default="")
    command.set_defaults(handler=_remote_action)
    command = environment.add_parser("carry-snippets", help="make an environment's Apache config carry the main site's config snippets (was carry_snippets.sh)")
    command.add_argument("site")
    command.set_defaults(handler=_carry_snippets)
    command = environment.add_parser("sync", help="sync settings to environments, or data to one site (was sync_env.sh)")
    command.add_argument("site")
    command.add_argument("source", help="main or an environment")
    command.add_argument("destination", help="environments separated by commas, or all; with --data one site, main too")
    command.add_argument("--data", help="content, media or content,media instead of settings")
    command.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    command.set_defaults(handler=_sync_environments)
    command = environment.add_parser("data-sync", help="settings <site>: set uSync by Sync setup; sync <from> <to>: sync data (was data_sync.sh)")
    command.add_argument("site")
    command.add_argument("action", choices=("settings", "sync"))
    command.add_argument("source", nargs="?", default="")
    command.add_argument("destinations", nargs="?", default="")
    # --dry-run, or anything else, which is refused as the script refused it
    command.add_argument("option", nargs=argparse.REMAINDER, help="--dry-run")
    command.set_defaults(handler=_data_sync)
    command = environment.add_parser("move", help="migrate or copy an environment to another folder or server, "
                                     "the destination in VNS_PROD_* (was move_env.sh)")
    command.add_argument("site")
    command.add_argument("environment", nargs="?", default="")
    command.set_defaults(handler=_move_environment)
    command = environment.add_parser("clear-logs", help="Umbraco's logs where the environment is (was clear_logs_prod.sh)")
    command.add_argument("site")
    command.set_defaults(handler=_clear_environment_logs)
    command = environment.add_parser("publish", help="publish the site to the environment in VNS_TARGET (was publish_prod.sh)")
    command.add_argument("site")
    command.add_argument("--project", default="", help="the project's name, as the environment was made with")
    command.add_argument("--local-publish", default="", help="the site's own publish folder, where a first publish takes its appsettings from")
    command.add_argument("--dry-run", action="store_true", help="Windows: build, and show what would change on the server")
    command.set_defaults(handler=_publish_environment)
    command = environment.add_parser("create", help="add an environment, from the admin's dialog (VNS_PROD_MODAL, VNS_PROD_*) or asked for (was create_prod.sh)")
    command.add_argument("site")
    command.add_argument("target", nargs="?", default="", help="remote, local or windows")
    for option in ("site-root", "binding", "project", "publish-folder", "admin-mail", "kind"):
        command.add_argument(f"--{option}", default="", help="as create_prod.sh was rendered with")
    command.set_defaults(handler=_create_environment)
    command = environment.add_parser("binding", help="change the binding and aliases where the environment is (was change_binding_prod.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("binding", nargs="?", default="")
    command.add_argument("aliases", nargs="?", default=None, help="the aliases, separated by commas or spaces; asked for when not given")
    command.add_argument("confirm", nargs="?", default="", help="--yes")
    command.add_argument("--yes", action="store_true")
    command.set_defaults(handler=_environment_binding)
    command = environment.add_parser("windows-file", help="read or save web.config or appsettings*.json on Windows (was windows_file.sh)")
    command.add_argument("site")
    command.add_argument("action", choices=("read", "save"))
    command.add_argument("file")
    command.set_defaults(handler=_windows_file)
    command = environment.add_parser("windows-files", help="one file manager operation on Windows, JSON on stdin (was windows_files.sh)")
    command.add_argument("site")
    command.set_defaults(handler=_windows_files)
    return root


def touched_sites(args):
    """The sites a command may have changed the state of; None for every site"""
    if args.group not in ("site", "env"):
        return []
    if args.command == "receive":
        return None
    names = [getattr(args, key, "") for key in ("site", "source", "main")]
    if args.command == "copy":
        # The new site's folder; env create's target is remote, local or windows
        names.append(args.target)
    if getattr(args, "arguments", None):
        # site create: the folder is its first argument
        names.append(args.arguments[0])
    return [name for name in names if isinstance(name, str) and name]


def keep_site_rights(system, args):
    """After a command that sets the modes in a site's folder, the site's group gets its rights again (phase 8b)"""
    if os.environ.get("VNS_WEB_ROOT") or (args.group, args.command) not in (("site", "fix-permissions"), ("site", "publish"), ("env", "publish")):
        return
    from harbor.server.users import keep_site
    for name in touched_sites(args) or []:
        try:
            keep_site(system, name)
        except (OSError, CommandFailed) as error:
            system.warn(f"The rights of {name}'s group were not set again: {error}")


def keep_state(system, args):
    """Harbor's copy of the sites' state stays equal to the site folders after a command (harbor/state)"""
    if os.environ.get("VNS_WEB_ROOT"):
        # Sites in a folder of their own (a test): the server's copy is not theirs
        return
    names = touched_sites(args)
    if names == []:
        # Loaded only now: a command on Harbor itself (self update, self uninstall) can have replaced or removed its code
        return
    from harbor.state import store
    try:
        problems = store.sync_all(system) if names is None else [problem for name in names for problem in store.sync(system, name)]
    except (OSError, ValueError) as error:
        problems = [str(error)]
    for problem in problems:
        system.warn(f"Harbor's copy of the state was not updated: {problem}")


def main(argv=None, system=None, keep=None):
    """keep: update Harbor's copy of the state after the command; by default only on the server itself (no system given)"""
    args = parser().parse_args(argv)
    keep = system is None if keep is None else keep
    system = system or System()
    try:
        args.handler(args, system)
        system.finished()
    except Stop as error:
        if str(error):
            system.warn(str(error))
        return error.code
    except CommandFailed as error:
        system.warn(str(error))
        return error.result.returncode or 1
    finally:
        if keep:
            keep_state(system, args)
            keep_site_rights(system, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
