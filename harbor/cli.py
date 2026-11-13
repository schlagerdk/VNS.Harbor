"""harbor <group> <command> [arguments]: everything the admin site and the scripts do, as one command.

Each command has the arguments its script had, and reads the same VNS_* settings from the environment, so a script
that has become a wrapper behaves as before.
"""
import argparse
import sys

from harbor.core.system import CommandFailed, System


def _servername(args, system):
    from harbor.server.apache import set_servername
    set_servername(system)


def parser():
    root = argparse.ArgumentParser(prog="harbor", description="VNS Harbor: web sites on this server, and their environments.")
    groups = root.add_subparsers(dest="group", required=True, metavar="<group>")

    server = groups.add_parser("server", help="the server itself").add_subparsers(dest="command", required=True, metavar="<command>")
    command = server.add_parser("set-servername", help="give Apache a global ServerName (was set_servername.sh)")
    command.set_defaults(handler=_servername)
    return root


def main(argv=None, system=None):
    args = parser().parse_args(argv)
    system = system or System()
    try:
        args.handler(args, system)
    except CommandFailed as error:
        system.warn(str(error))
        return error.result.returncode or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
