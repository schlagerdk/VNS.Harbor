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
    copy_site(system, args.source, args.target, args.binding, args.database or "")


def _setup_site(args, system):
    from harbor.sites.setup import setup_site
    setup_site(system, args.site, args.binding, args.certificate, args.environment)


def _delete_site(args, system):
    from harbor.sites.delete import delete_site
    delete_site(system, args.site, args.certificates, args.scope, os.environ.get("VNS_TARGET", ""))


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

    environment = groups.add_parser("env", help="an environment of a site, chosen with VNS_TARGET").add_subparsers(dest="command", required=True, metavar="<command>")
    command = environment.add_parser("action", help="issue_certificate or change_env where the environment is (was prod_action.sh)")
    command.add_argument("site", nargs="?", default="")
    command.add_argument("action", nargs="?", default="")
    command.add_argument("argument", nargs="?", default="", help="the environment for change_env, or --yes")
    command.add_argument("--yes", action="store_true", help="no question before a certificate")
    command.set_defaults(handler=_environment_action)
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
