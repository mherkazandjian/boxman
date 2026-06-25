"""
Argparse parser construction for the boxman CLI.

Extracted from ``scripts/app.py`` in Phase 2.5 of the review plan
(see /home/mher/.claude/plans/) to keep the argparse wiring separate
from the orchestration in ``main()``. The public surface is just
:func:`parse_args`, which returns the top-level
:class:`argparse.ArgumentParser` ready for ``.parse_known_args()``.

Two local helpers (``export_config`` / ``import_config``) are still
imported lazily inside :func:`parse_args` to avoid a circular import
— they'll migrate here once the remaining app.py split lands in a
follow-up pass.
"""

from __future__ import annotations

import argparse
from argparse import RawTextHelpFormatter
from datetime import datetime, timezone

import boxman
from boxman.manager import BoxmanManager


#: Default snapshot name — current UTC timestamp formatted for display.
#: Evaluated at module-import time (same semantics as the original
#: module-level constant in app.py).
snap_name = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def parse_args():
    # Lazy-imported here (not at module scope) to avoid a circular import
    # with boxman.scripts.app, which imports parse_args from this module.
    from boxman.scripts.app import export_config, import_config  # noqa: F401

    parser = argparse.ArgumentParser(
        description=(
            f"Boxman version {boxman.metadata.version}\n"
            "Virtualbox vboxmanage wrapper and infrastructure as code manager\n"
            "\n"
            "usage example\n"
            "\n"
            "   list\n"
            "       # list all projects that have been provisioned\n"
            "       $ boxman list\n"
            "\n"
            "   provision\n"
            "       # provision the configuration in the default config file (conf.yml)\n"
            "       $ boxman provision\n"
            "\n"
            "       # provision using the docker-compose runtime environment\n"
            "       $ boxman --runtime docker-compose provision\n"
            "\n"
            "   snapshot\n"
            "\n"
            "     list\n"
            "       # list snapshots\n"
            "       $ boxman snapshot list\n"
            "\n"
            "     delete\n"
            "       # delete snapshots\n"
            "       $ boxman snapshot delete\n"
            "\n"
            "     take\n"
            "       # snapshot all vms in the default config file\n"
            "       $ boxman snapshot take\n"
            "\n"
            "       # snapshot one or more vms\n"
            "       $ boxman snapshot take --vm=myvm1\n"
            "       $ boxman snapshot take --vm=myvm1,myvm2\n"
            "\n"
            "       # snapshot and set a name for the snapshot (all vms get the same snapshot name)\n"
            "       $ boxman snapshot take --name=mystate1\n"
            "\n"
            "     restore\n"
            "       # restore all vms in the default config file\n"
            "       $ boxman snapshot restore --name=mystate1\n"
            "\n"
            "       # restore one or more vms\n"
            "       $ boxman snapshot restore --vm=myvm1\n"
            "       $ boxman snapshot restore  --vm=myvm1,myvm2\n"
            "\n"
        ),
        formatter_class=RawTextHelpFormatter
    )

    parser.add_argument(
        '--conf',
        type=str,
        help='the name of the configuration file',
        dest='conf',
        default='conf.yml'
    )

    parser.add_argument(
        '--boxman-conf',
        type=str,
        help='the name of the boxman configuration file',
        dest='boxman_conf',
        default='~/.config/boxman/boxman.yml'
    )

    parser.add_argument(
        '--runtime',
        type=str,
        help=(
            'the runtime environment in which to execute provider commands.\n'
            'overrides the "runtime" setting in boxman.yml.\n'
            '  local          - run provider commands directly on the host (default)\n'
            '  docker         - run inside the boxman docker-compose container\n'
        ),
        dest='runtime',
        default=None,
        choices=['local', 'docker']
    )

    parser.add_argument(
        '--version',
        action='count',
        default=0,
        help='display the version and exit'
    )

    subparsers = parser.add_subparsers(help="sub-commands for boxman")

    #
    # sub parser for importing images
    #
    parser_import_image = subparsers.add_parser('import-image', help='import an image')
    parser_import_image.set_defaults(func=BoxmanManager.import_image)

    parser_import_image.add_argument(
        '--uri',
        type=str,
        help='the URI of the manifest of the image to import',
        dest='manifest_uri',
        required=True
    )

    parser_import_image.add_argument(
        '--name',
        type=str,
        help='the name to assign to the imported vm',
        dest='vm_name',
        required=False    # the default is used from the manifest
    )

    parser_import_image.add_argument(
        '--directory',
        type=str,
        help='the directory to download/extract the image into',
        dest='vm_dir',
        required=False
    )

    parser_import_image.add_argument(
        '--provider',
        type=str,
        help='the provider to import the image into',
        dest='provider',
        required=False,
        choices=['virtualbox', 'libvirt'])   # figure out how to automate this with the
                                             # supported providers list below

    #
    # sub parser for the 'image' subcommand (OCI registry image operations)
    #
    parser_image = subparsers.add_parser(
        'image',
        help='OCI registry image operations (push, ...)')
    subparsers_image = parser_image.add_subparsers(help='sub-commands for boxman image')

    #
    # sub parser for the 'image push' subsubcommand
    #
    parser_image_push = subparsers_image.add_parser(
        'push',
        help='push a qcow2 image (and optional metadata) to an OCI registry')
    parser_image_push.set_defaults(func=BoxmanManager.push_image)
    parser_image_push.add_argument(
        'image_ref',
        type=str,
        help='OCI image reference (e.g. registry.example.com/repo:tag)'
    )
    parser_image_push.add_argument(
        '--qcow2',
        type=str,
        help='path to the qcow2 disk image file to push',
        dest='qcow2',
        required=True
    )
    parser_image_push.add_argument(
        '--metadata',
        type=str,
        help='optional path to a vmimage.json metadata file',
        dest='metadata',
        required=False
    )

    #
    # sub parser for the 'image inspect' subsubcommand
    #
    parser_image_inspect = subparsers_image.add_parser(
        'inspect',
        help='inspect an OCI image reference (manifest + vmimage.json metadata)')
    parser_image_inspect.set_defaults(func=BoxmanManager.inspect_image)
    parser_image_inspect.add_argument(
        'image_ref',
        type=str,
        help='OCI image reference (e.g. oci://registry.example.com/repo:tag)'
    )

    #
    # sub parser for creating templates from cloud images
    #
    parser_create_templates = subparsers.add_parser(
        'create-templates',
        help='create template VMs from cloud images using cloud-init')
    parser_create_templates.set_defaults(func=BoxmanManager.create_templates)
    parser_create_templates.add_argument(
        '--templates',
        type=str,
        help='comma-separated list of template keys to create (default: all)',
        dest='template_names',
        default=None
    )
    parser_create_templates.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='force creation even if VM already exists',
        dest='force'
    )

    #
    # sub parser for listing the registered projects
    #
    parser_list = subparsers.add_parser('list', help='list all registered projects')
    parser_list.set_defaults(func=BoxmanManager.list_projects)

    list_format_group = parser_list.add_mutually_exclusive_group()
    list_format_group.add_argument(
        '--pretty', '-p',
        type=str,
        nargs='?',
        const='plain',
        default=None,
        choices=['plain', 'table'],
        help='display in a human-readable format without logger prefixes (plain or table)',
        dest='pretty'
    )
    list_format_group.add_argument(
        '--json',
        action='store_true',
        default=False,
        help='output the project list as JSON',
        dest='json'
    )

    parser_list.add_argument(
        '--color',
        type=str,
        default='yes',
        choices=['yes', 'no'],
        help='enable or disable colored output (default: yes)',
        dest='color'
    )

    #
    # sub parser for provisioning a configuration
    #
    parser_prov = subparsers.add_parser('provision', help='provision a configuration')
    parser_prov.set_defaults(func=BoxmanManager.provision)
    parser_prov.add_argument(
        '--docker-compose',
        action='store_true',
        default=False,
        help='provision using the docker-compose setup',
        dest='docker_compose'
    )
    parser_prov.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='if VMs already exist, deprovision them first and then provision',
        dest='force'
    )
    parser_prov.add_argument(
        '--rebuild-templates',
        action='store_true',
        default=False,
        help='force-rebuild all templates (destroy and recreate) before provisioning',
        dest='rebuild_templates'
    )

    #
    # sub parser for the 'up' subcommand
    #
    parser_up = subparsers.add_parser(
        'up',
        help='bring up the infrastructure: provision if not created, start if powered off')
    parser_up.set_defaults(func=BoxmanManager.up)
    parser_up.add_argument(
        '--docker-compose',
        action='store_true',
        default=False,
        help='use the docker-compose setup',
        dest='docker_compose'
    )
    parser_up.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='if VMs already exist, deprovision them first and then provision',
        dest='force'
    )
    parser_up.add_argument(
        '--rebuild-templates',
        action='store_true',
        default=False,
        help='force-rebuild all templates (destroy and recreate) before provisioning',
        dest='rebuild_templates'
    )

    #
    # sub parser for the 'update' subcommand
    #
    parser_update = subparsers.add_parser(
        'update',
        help='apply config changes to already-provisioned VMs (CPU, memory, disks, add/remove VMs)')
    parser_update.set_defaults(func=BoxmanManager.update)
    parser_update.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='show what would change without applying modifications',
        dest='dry_run'
    )
    parser_update.add_argument(
        '--docker-compose',
        action='store_true',
        default=False,
        help='use the docker-compose setup',
        dest='docker_compose'
    )
    parser_update.add_argument(
        '--yes', '-y',
        action='store_true',
        default=False,
        help='skip confirmation prompt for VM removal',
        dest='yes'
    )

    #
    # sub parser for the 'down' subcommand
    #
    parser_down = subparsers.add_parser(
        'down',
        help='bring down the infrastructure: save or suspend the state of all VMs')
    parser_down.set_defaults(func=BoxmanManager.down)
    parser_down.add_argument(
        '--suspend',
        action='store_true',
        default=False,
        help='suspend (pause) VMs instead of saving their state to disk',
        dest='suspend'
    )

    #
    # sub parser for destroying the runtime environment
    #
    parser_destroy_rt = subparsers.add_parser(
        'destroy-runtime',
        help='destroy the docker-compose runtime environment and clean up .boxman')
    parser_destroy_rt.add_argument(
        '--auto-accept', '-y', action='store_true', default=False,
        help='skip the confirmation prompt and proceed immediately')
    parser_destroy_rt.set_defaults(func=BoxmanManager.destroy_runtime)

    #
    # sub parser for the full-teardown 'destroy' command
    #
    parser_destroy = subparsers.add_parser(
        'destroy',
        help=('nuke everything provisioned by this config: VMs, networks, '
              'generated files, the docker runtime (if used) and the '
              'workspace workdir. Prompts [y/N] unless -y is given.'))
    parser_destroy.add_argument(
        '--auto-accept', '-y', action='store_true', default=False,
        help='skip the confirmation prompt and proceed immediately')
    parser_destroy.add_argument(
        '--templates', action='store_true', default=False,
        help=('also remove template workdirs (~/boxman-templates by '
              'default, or a per-template workdir override). Off by '
              'default because templates are often shared across projects.'))
    parser_destroy.set_defaults(func=BoxmanManager.destroy)

    #
    # sub parser for deprovisioning a configuration
    #
    parser_deprov = subparsers.add_parser('deprovision', help='deprovision a configuration')
    parser_deprov.set_defaults(func=BoxmanManager.deprovision)
    parser_deprov.add_argument(
        '--docker-compose',
        action='store_true',
        default=False,
        help='deprovision using the docker-compose setup',
        dest='docker_compose'
    )
    parser_deprov.add_argument(
        '--cleanup',
        action='store_true',
        default=False,
        help='also remove provisioned files, SSH keys, and empty directories',
        dest='cleanup'
    )

    ##
    ## sub parser for the 'deprovision cluster' subsubcommand
    ##
    #parser_deprov_config = subparsers_deprov.add_parser('config', help='deprovision the whole cluster')
    #parser_deprov_config.set_defaults(func=BoxmanManager.deprovision)

    #
    # sub parser for the 'snapshot' subcommand
    #
    parser_snap = subparsers.add_parser('snapshot', help='manage snapshots the state of the vms')

    subparsers_snap = parser_snap.add_subparsers(
        help="sub-commands for boxman snapshot")

    #
    # sub parser for the 'snapshot take' subsubcommand
    #
    parser_snap_take = subparsers_snap.add_parser('take', help='take a snapshot')
    parser_snap_take.set_defaults(func=BoxmanManager.snapshot_take)
    parser_snap_take.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_snap_take.add_argument(
        '--cluster',
        type=str,
        default=None,
        dest='cluster',
        help='restrict the snapshot to a single cluster',
    )
    parser_snap_take.add_argument(
        '--name',
        type=str,
        help='the name of the snapshot',
        dest='snapshot_name',
        default=snap_name
    )
    parser_snap_take.add_argument(
        "--description",
        '-m',
        type=str,
        help='the description of the snapshot',
        dest='snapshot_descr',
        default=f'boxman snapshot {snap_name}'
    )
    parser_snap_take.add_argument(
        '--live',
        action='store_true',
        help='take a snapshot with stopping the vm',
    )
    parser_snap_take.add_argument(
        '--no-live',
        action='store_false',
        help='take a snapshot without stopping the vm',
        dest='live',
    )
    parser_snap_take.add_argument(
        '--compress-memory',
        action='store_true',
        dest='compress_memory',
        help='zstd-compress the memory .raw file after the snapshot is '
             'created (decompressed transparently on restore)',
    )
    parser_snap_take.add_argument(
        '--memory-compress-level',
        type=int,
        default=3,
        dest='memory_compress_level',
        help='zstd compression level (default 3 — sweet spot)',
    )

    #
    # sub parser for the 'snapshot list' subsubcommand
    #
    parser_snap_list = subparsers_snap.add_parser('list', help='list snapshots')
    parser_snap_list.set_defaults(func=BoxmanManager.snapshot_list)
    parser_snap_list.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'snapshot log' subsubcommand
    #
    parser_snap_log = subparsers_snap.add_parser(
        'log',
        help='git-log-style aggregated snapshot view across all vms')
    parser_snap_log.set_defaults(func=BoxmanManager.snapshot_log)
    parser_snap_log.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all',
    )
    parser_snap_log.add_argument(
        '-n', '--max',
        type=int,
        default=None,
        dest='max_count',
        help='show at most N entries (newest first; pair with --reverse '
             'for the oldest N)',
    )
    parser_snap_log.add_argument(
        '--json',
        action='store_true',
        dest='as_json',
        help='emit machine-readable JSON instead of the text table',
    )
    parser_snap_log.add_argument(
        '--reverse',
        action='store_true',
        dest='reverse',
        help='oldest first',
    )
    parser_snap_log.add_argument(
        '--no-graph',
        action='store_true',
        dest='no_graph',
        help='suppress the leftmost graph column (useful for piping)',
    )

    #
    # sub parser for the 'snapshot restore' subsubcommand
    #
    parser_snap_restore = subparsers_snap.add_parser('restore', help='restore the state of vms from snapshot')
    parser_snap_restore.set_defaults(func=BoxmanManager.snapshot_restore)
    parser_snap_restore.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_snap_restore.add_argument(
        '--cluster',
        type=str,
        default=None,
        dest='cluster',
        help='restrict the restore to a single cluster',
    )
    parser_snap_restore.add_argument(
        '--name',
        type=str,
        help='the name of the snapshot',
        dest='snapshot_name',
        default=None
    )

    #
    # sub parser for the 'snapshot delete' subsubcommand
    #
    parser_snap_delete = subparsers_snap.add_parser('delete', help='delete a snapshot')
    parser_snap_delete.set_defaults(func=BoxmanManager.snapshot_delete)
    parser_snap_delete.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_snap_delete.add_argument(
        '--cluster',
        type=str,
        default=None,
        dest='cluster',
        help='restrict the delete to a single cluster',
    )
    parser_snap_delete.add_argument(
        '--name',
        type=str,
        help='the name of the snapshot',
        dest='snapshot_name',
        default=None
    )

    #
    # sub parser for the 'snapshot collapse' subsubcommand
    #
    parser_snap_collapse = subparsers_snap.add_parser(
        'collapse',
        help='merge snapshots newer than --to into the live head '
             '(target snapshot remains revertable)')
    parser_snap_collapse.set_defaults(func=BoxmanManager.snapshot_collapse)
    parser_snap_collapse.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all',
    )
    parser_snap_collapse.add_argument(
        '--to',
        type=str,
        required=True,
        dest='target',
        help='oldest snapshot to keep revertable; everything between '
             'this and the live head is merged into the head and dropped',
    )
    parser_snap_collapse.add_argument(
        '--no-shutdown',
        action='store_true',
        dest='no_shutdown',
        help='skip running VMs instead of auto-shutting them down '
             '(rebase requires the VM offline)',
    )
    parser_snap_collapse.add_argument(
        '--dry-run',
        action='store_true',
        dest='dry_run',
        help='print what would be merged; no writes',
    )
    parser_snap_collapse.add_argument(
        '-y', '--yes',
        action='store_true',
        dest='yes',
        help='skip the destructive-action confirmation prompt',
    )

    #
    # sub parser for the top-level 'restore' subcommand
    # (shortcut for 'snapshot restore' with no --name: restores the latest snapshot)
    #
    parser_restore = subparsers.add_parser(
        'restore',
        help='restore all VMs to their latest snapshot')
    parser_restore.set_defaults(func=BoxmanManager.snapshot_restore, snapshot_name=None)

    #
    # sub parser for the 'storage' subcommand
    #
    parser_storage = subparsers.add_parser(
        'storage', help='inspect and reclaim qcow2 disk space')

    subparsers_storage = parser_storage.add_subparsers(
        help='sub-commands for boxman storage')

    #
    # sub parser for the 'storage df' subsubcommand
    #
    parser_storage_df = subparsers_storage.add_parser(
        'df', help='show per-vm disk usage and reclaim estimate')
    parser_storage_df.set_defaults(func=BoxmanManager.storage_df)
    parser_storage_df.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all',
    )

    #
    # sub parser for the 'storage trim' subsubcommand
    #
    parser_storage_trim = subparsers_storage.add_parser(
        'trim',
        help='run fstrim inside running guests via qemu-guest-agent')
    parser_storage_trim.set_defaults(func=BoxmanManager.storage_trim)
    parser_storage_trim.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all',
    )
    parser_storage_trim.add_argument(
        '--dry-run',
        action='store_true',
        dest='dry_run',
        help='print what would be done; do not run fstrim',
    )

    #
    # sub parser for the 'storage compact' subsubcommand
    #
    parser_storage_compact = subparsers_storage.add_parser(
        'compact',
        help='reclaim qcow2 space (sparsify or qemu-img convert)')
    parser_storage_compact.set_defaults(func=BoxmanManager.storage_compact)
    parser_storage_compact.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all',
    )
    parser_storage_compact.add_argument(
        '--method',
        choices=['auto', 'sparsify', 'convert', 'convert-compressed'],
        default='auto',
        dest='method',
        help='compaction method (auto picks sparsify when snapshots exist, '
             'convert otherwise)',
    )
    parser_storage_compact.add_argument(
        '--no-shutdown',
        action='store_true',
        dest='no_shutdown',
        help='do not auto-shutdown running vms; skip them instead',
    )
    parser_storage_compact.add_argument(
        '--drop-snapshots',
        action='store_true',
        dest='drop_snapshots',
        help='allow chain-flattening methods (convert/convert-compressed) '
             'when snapshots exist',
    )
    parser_storage_compact.add_argument(
        '--dry-run',
        action='store_true',
        dest='dry_run',
        help='print before/after estimates; do not write',
    )

    #
    # sub parser for the 'storage optimize' subsubcommand
    #
    parser_storage_optimize = subparsers_storage.add_parser(
        'optimize',
        help='trim guests then compact qcow2 files (orchestrator)')
    parser_storage_optimize.set_defaults(func=BoxmanManager.storage_optimize)
    parser_storage_optimize.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all',
    )
    parser_storage_optimize.add_argument(
        '--method',
        choices=['auto', 'sparsify', 'convert', 'convert-compressed'],
        default='auto',
        dest='method',
    )
    parser_storage_optimize.add_argument(
        '--skip-trim',
        action='store_true',
        dest='skip_trim',
        help='skip the guest-side fstrim phase',
    )
    parser_storage_optimize.add_argument(
        '--skip-compact',
        action='store_true',
        dest='skip_compact',
        help='skip the host-side qcow2 compact phase',
    )
    parser_storage_optimize.add_argument(
        '--no-shutdown',
        action='store_true',
        dest='no_shutdown',
        help='do not auto-shutdown running vms during compact',
    )
    parser_storage_optimize.add_argument(
        '--drop-snapshots',
        action='store_true',
        dest='drop_snapshots',
    )
    parser_storage_optimize.add_argument(
        '--dry-run',
        action='store_true',
        dest='dry_run',
    )

    #
    # sub parser for the 'storage compress-snapshots' subsubcommand
    #
    parser_storage_compress = subparsers_storage.add_parser(
        'compress-snapshots',
        help='zstd-compress (or decompress) snapshot memory .raw files')
    parser_storage_compress.set_defaults(
        func=BoxmanManager.storage_compress_snapshots)
    parser_storage_compress.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all',
    )
    parser_storage_compress.add_argument(
        '--level',
        type=int,
        default=3,
        dest='level',
        help='zstd compression level (default 3)',
    )
    parser_storage_compress.add_argument(
        '--decompress',
        action='store_true',
        dest='decompress',
        help='decompress .raw.zst back to .raw instead of compressing',
    )

    #
    # sub parser for the 'control' subcommand
    #
    parser_ctrl = subparsers.add_parser('control', help='control the state of vms')

    subparsers_ctrl = parser_ctrl.add_subparsers(
        help="sub-commands for boxman control")

    #
    # sub parser for the 'control suspend' subsubcommand
    #
    parser_ctrl_suspend = subparsers_ctrl.add_parser('suspend', help='suspend vms')
    parser_ctrl_suspend.set_defaults(func=BoxmanManager.suspend_vm)
    parser_ctrl_suspend.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'control resume' subsubcommand
    #
    parser_ctrl_resume = subparsers_ctrl.add_parser('resume', help='resume vms')
    parser_ctrl_resume.set_defaults(func=BoxmanManager.resume_vm)
    parser_ctrl_resume.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'control save' subsubcommand
    #
    parser_ctrl_save = subparsers_ctrl.add_parser('save', help='save the state of vms')
    parser_ctrl_save.set_defaults(func=BoxmanManager.save_vm)
    parser_ctrl_save.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )

    #
    # sub parser for the 'control start' subsubcommand
    #
    parser_ctrl_start = subparsers_ctrl.add_parser('start', help='start the vms')
    parser_ctrl_start.set_defaults(func=BoxmanManager.start_vm)
    parser_ctrl_start.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_ctrl_start.add_argument(
        '--restore',
        action='store_true',
        default=False,
        help='restore the saved state of the vm before starting',
        dest='restore'
    )

    #
    # sub parser for the 'export' subcommand
    #
    parser_export = subparsers.add_parser('export', help='export the vms')
    parser_export.set_defaults(func=export_config)
    parser_export.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_export.add_argument(
        '--path',
        type=str,
        help='the names of the vms as a csv list',
        dest='path',
        default=None
    )

    #
    # sub parser for the 'import' subcommand
    #
    parser_import_image = subparsers.add_parser('import', help='import the vms')
    parser_import_image.set_defaults(func=import_config)
    parser_import_image.add_argument(
        '--vms',
        type=str,
        help='the names of the vms as a csv list',
        dest='vms',
        default='all'
    )
    parser_import_image.add_argument(
        '--path',
        type=str,
        help='the names of the vms as a csv list',
        dest='path',
    )

    #
    # sub parser for the 'run' subcommand
    #
    parser_run = subparsers.add_parser(
        'run',
        help='run tasks with the workspace environment loaded',
        description=(
            "Run named tasks or ad-hoc commands with environment variables\n"
            "loaded from the workspace env file (env.sh).\n"
            "\n"
            "examples:\n"
            "    # list available tasks\n"
            "    $ boxman run --list\n"
            "\n"
            "    # run a named task\n"
            "    $ boxman run ping\n"
            "\n"
            "    # run a task with extra arguments\n"
            "    $ boxman run site -- --limit foo --tags=bar\n"
            "\n"
            "    # run an ad-hoc command with the workspace env loaded\n"
            "    $ boxman run --cmd 'ansible all -m ping'\n"
        ),
        formatter_class=RawTextHelpFormatter
    )
    parser_run.set_defaults(func=BoxmanManager.run_task)

    parser_run.add_argument(
        'task_name',
        type=str,
        nargs='?',
        default=None,
        help='name of the task to run (defined in conf.yml tasks section)'
    )
    parser_run.add_argument(
        'extra_args',
        nargs='*',
        default=[],
        help='extra arguments passed to the task command'
    )
    parser_run.add_argument(
        '--list', '-l',
        action='store_true',
        default=False,
        help='list available tasks',
        dest='list_tasks'
    )
    parser_run.add_argument(
        '--cmd',
        type=str,
        default=None,
        help='run an ad-hoc command with the workspace environment loaded',
        dest='cmd'
    )
    parser_run.add_argument(
        '--ansible-flags',
        type=str,
        default=None,
        help='flags passed to ansible for --cmd',
        dest='ansible_flags'
    )
    parser_run.add_argument(
        '--cluster',
        type=str,
        default=None,
        help='cluster name to scope the workspace environment to',
        dest='cluster'
    )

    # ── ps ───────────────────────────────────────────────────────────
    parser_ps = subparsers.add_parser(
        'ps',
        help='show the state of VMs in the project',
        description=(
            "Display the current state of all VMs defined in the project\n"
            "configuration.\n"
            "\n"
            "examples:\n"
            "    $ boxman ps\n"
            "    $ boxman ps -p   # include provider-specific info (virsh Id, virsh Name)\n"
        ),
        formatter_class=RawTextHelpFormatter
    )
    parser_ps.set_defaults(func=BoxmanManager.ps)
    parser_ps.add_argument(
        '-p',
        action='store_true',
        default=False,
        help='show provider-specific information (virsh Id, virsh Name)',
        dest='provider_info'
    )
    parser_ps.add_argument(
        '--json',
        action='store_true',
        default=False,
        help='output as JSON instead of a table',
        dest='json'
    )

    # ── conf ─────────────────────────────────────────────────────────
    parser_conf = subparsers.add_parser(
        'conf',
        help='show the effective configuration',
        description=(
            "Display the effective merged configuration that boxman will use.\n"
            "\n"
            "Shows the merged provider config (defaults + boxman.yml + conf.yml)\n"
            "and the rendered project config (conf.rendered.yml).\n"
            "\n"
            "examples:\n"
            "    $ boxman conf\n"
            "    $ boxman conf --json\n"
        ),
        formatter_class=RawTextHelpFormatter
    )
    parser_conf.set_defaults(func=BoxmanManager.show_conf)
    parser_conf.add_argument(
        '--json',
        action='store_true',
        default=False,
        help='output as JSON',
        dest='json'
    )

    # ── ssh ──────────────────────────────────────────────────────────
    parser_ssh = subparsers.add_parser(
        'ssh',
        help='ssh into a VM',
        description=(
            "Open an interactive SSH session to a VM.\n"
            "\n"
            "Defaults to the gateway host (first VM) when no name is given.\n"
            "\n"
            "examples:\n"
            "    $ boxman ssh\n"
            "    $ boxman ssh cluster_1_node02\n"
            "    $ boxman ssh node02\n"
        ),
        formatter_class=RawTextHelpFormatter
    )
    parser_ssh.set_defaults(func=BoxmanManager.ssh_session)

    parser_ssh.add_argument(
        'vm_name',
        type=str,
        nargs='?',
        default=None,
        help='VM name to ssh into (default: gateway host)'
    )
    parser_ssh.add_argument(
        '--cluster',
        type=str,
        default=None,
        help='cluster name to scope the workspace environment to',
        dest='cluster'
    )

    # ── pxe-boot ─────────────────────────────────────────────────────
    parser_pxe = subparsers.add_parser(
        'pxe-boot',
        help='set a VM to network-boot and optionally wait for SSH',
        description=(
            "Set a VM's boot order to [network, hd], start it, and\n"
            "optionally poll for SSH availability after PXE provisioning.\n"
            "\n"
            "Requires a Cobbler (or compatible) PXE provisioning server on\n"
            "the same libvirt network as the VM.\n"
            "\n"
            "examples:\n"
            "    # Boot from network, don't wait\n"
            "    $ boxman pxe-boot --vm pxe-test01\n"
            "\n"
            "    # Boot from network and wait for SSH, then restore boot order\n"
            "    $ boxman pxe-boot --vm pxe-test01 --expected-ip 192.168.123.50 "
            "--restore-after\n"
        ),
        formatter_class=RawTextHelpFormatter
    )
    parser_pxe.set_defaults(func=BoxmanManager.pxe_boot)
    parser_pxe.add_argument(
        '--vm',
        type=str,
        required=True,
        help='full domain name of the VM to PXE boot',
        dest='vm'
    )
    parser_pxe.add_argument(
        '--expected-ip',
        type=str,
        default=None,
        help='IP address to poll for SSH after the OS is installed',
        dest='expected_ip'
    )
    parser_pxe.add_argument(
        '--wait-timeout',
        type=int,
        default=600,
        help='maximum seconds to wait for SSH (default: 600)',
        dest='wait_timeout'
    )
    parser_pxe.add_argument(
        '--restore-after',
        action='store_true',
        default=False,
        help='restore boot order to [hd] after SSH becomes available',
        dest='restore_after'
    )

    # ── netlab (containerlab) ────────────────────────────────────────
    parser_netlab = subparsers.add_parser(
        'netlab',
        help='manage the containerlab network-gear topology',
        description=(
            "Drive the containerlab lab declared under the 'containerlab:'\n"
            "block in conf.yml. Useful for tearing the lab down and bringing\n"
            "it back up without re-provisioning libvirt VMs.\n"
            "\n"
            "examples:\n"
            "    $ boxman netlab deploy\n"
            "    $ boxman netlab destroy\n"
            "    $ boxman netlab inspect\n"
            "    $ boxman netlab ssh sw1\n"
            "    $ $(boxman netlab ssh sw1)    # drop into the vendor CLI\n"
        ),
        formatter_class=RawTextHelpFormatter,
    )
    subparsers_netlab = parser_netlab.add_subparsers(
        help="sub-commands for boxman netlab")

    parser_netlab_deploy = subparsers_netlab.add_parser(
        'deploy', help='render topology and deploy the containerlab lab')
    parser_netlab_deploy.set_defaults(func=BoxmanManager.netlab_deploy)

    parser_netlab_destroy = subparsers_netlab.add_parser(
        'destroy', help='tear down the containerlab lab (leaves VMs alone)')
    parser_netlab_destroy.set_defaults(func=BoxmanManager.netlab_destroy)

    parser_netlab_inspect = subparsers_netlab.add_parser(
        'inspect', help='print containerlab inspect --format json')
    parser_netlab_inspect.set_defaults(func=BoxmanManager.netlab_inspect)

    parser_netlab_ssh = subparsers_netlab.add_parser(
        'ssh',
        help='print the ssh command for a lab node (e.g. $(boxman netlab ssh sw1))'
    )
    parser_netlab_ssh.set_defaults(func=BoxmanManager.netlab_ssh)
    parser_netlab_ssh.add_argument(
        'node',
        type=str,
        help='lab node name as declared in containerlab.topology.nodes',
    )
    parser_netlab_ssh.add_argument(
        '--user',
        type=str,
        default=None,
        help='override the ssh user (default: node login-user or "admin")',
        dest='user',
    )

    return parser
