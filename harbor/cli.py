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
    command = server.add_parser("set-servername", help="give Apache a global ServerName (was set_servername.sh)")
    command.set_defaults(handler=_servername)
    command = server.add_parser("install-apache-conf", help="the server-wide Apache configs (was install_apache_conf.sh)")
    command.set_defaults(handler=_apache_conf)
    command = server.add_parser("install-tls-policy", help="the shared TLS policy (was install_tls_policy.sh)")
    command.add_argument("--start-if-stopped", action="store_true")
    command.set_defaults(handler=_tls_policy)

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
    command = site.add_parser("binding", help="change the binding and aliases of the site here (was change_binding.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("binding", nargs="?", default="", help="the new binding, or * to answer any name")
    command.add_argument("aliases", nargs="?", default=None, help="the aliases, separated by commas or spaces; asked for when not given")
    command.add_argument("confirm", nargs="?", default="", help="--yes")
    command.add_argument("--yes", action="store_true")
    command.set_defaults(handler=_change_binding)

    environment = groups.add_parser("env", help="an environment of a site, chosen with VNS_TARGET").add_subparsers(dest="command", required=True, metavar="<command>")
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
                                     "shell, files or config of an environment on another Linux server (was remote_site.sh)")
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


def main(argv=None, system=None):
    args = parser().parse_args(argv)
    system = system or System()
    try:
        args.handler(args, system)
    except Stop as error:
        if str(error):
            system.warn(str(error))
        return error.code
    except CommandFailed as error:
        system.warn(str(error))
        return error.result.returncode or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
