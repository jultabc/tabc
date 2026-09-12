"""tabm: management command names, reusing the existing signed request handlers.

This command split is not an authorization boundary. Key recovery alone is
local-only; disable/enable/purge retain the daemon's existing authorization.
"""

from argparse import Namespace

from . import cli


def change_node(a):
    args = Namespace(node=a.target, actor=a.node, by=a.by,
                     purge=a.cmd == "purge", yes=getattr(a, "yes", False))
    if a.cmd == "enable":
        cli.fn_restore(args)
    else:
        cli.fn_rm(args)


def rotate_key(a):
    # Local key recovery deliberately has no HTTP equivalent.
    from .bus import cmd_rotate_key

    cmd_rotate_key(a)


def commands():
    specs = {
        "config": (cli.fn_config, "read or set the local owner email", [
            (("--email",), dict(default=None)),
        ]),
        "list": (cli.fn_who, "list active nodes and pending counts", [
            (("--node",), dict(required=True, help="acting node")),
        ]),
        "rotate-key": (rotate_key, "local-only public key recovery; not a remote request", [
            (("node",), dict(help="target node")),
            (("pubkey",), dict(help="replacement public key")),
            (("--by",), dict(required=True, help="operator recorded in the audit")),
        ]),
    }
    for action, description in (
        ("disable", "disable a node, retaining its history"),
        ("enable", "enable a previously disabled node"),
        ("purge", "permanently delete a node only if it has no history"),
    ):
        args = [
            (("--node",), dict(required=True, help="acting node signing this request")),
            (("--target",), dict(required=True, help="node being changed")),
            (("--by",), dict(default=None, help="audit label, not authentication")),
        ]
        if action != "enable":
            args.append((("--yes",), dict(action="store_true", help="confirm the operation")))
        specs[action] = (change_node, description, args)
    return specs


def build_parser():
    return cli.build_parser(prog="tabm", commands=commands())


def main():
    args = build_parser().parse_args()
    commands()[args.cmd][0](args)


if __name__ == "__main__":
    main()
