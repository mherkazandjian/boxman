"""Template, base-image, ISO, and OCI image handling for BoxmanManager."""


import hashlib
import os
import shutil
from urllib.parse import urlparse

from boxman.image_cache import ImageCache
from boxman.providers.libvirt.commands import VirshCommand
from boxman.utils.http_download import download_url
from boxman.utils.shell import run


class ImagesMixin:

    def import_image(self, cli_args) -> None:
        """
        Import an image into the provider's storage.

        :param manager: The instance of the BoxmanManager
        :param cli_args: The parsed arguments from the cli
        """
        # Phase 1 (#49): stays on the default session — import-image is a
        # single-provider flow (the type comes from the manifest / CLI).
        self.provider.import_image(
            manifest_uri=cli_args.manifest_uri,
            vm_name=cli_args.vm_name,
            vm_dir=cli_args.vm_dir,
            manifest_local_path=getattr(cli_args, 'manifest_local_path', None),
        )

    def push_image(self, cli_args) -> None:
        """
        Push a qcow2 image (and optional metadata) to an OCI registry.

        Provider-agnostic — delegates to the ``oras`` CLI. ``self`` is unused
        but kept for signature consistency with the other CLI dispatchers.
        """
        from boxman.providers.libvirt.oci_push import push_oci_image
        try:
            push_oci_image(
                image_ref=cli_args.image_ref,
                qcow2_path=cli_args.qcow2,
                metadata_path=getattr(cli_args, 'metadata', None),
            )
            print(f"successfully pushed image to {cli_args.image_ref}", flush=True)
        except (ValueError, RuntimeError) as exc:
            print(f"error pushing image: {exc}", flush=True)
            raise SystemExit(1) from exc

    def inspect_image(self, cli_args) -> None:
        """
        Inspect an OCI image reference: print its manifest summary and, when a
        ``vmimage.json`` sidecar is present, its metadata.

        Provider-agnostic — delegates to the ``oras`` CLI (manifest fetch, no
        full blob download). ``self`` is unused but kept for signature
        consistency with the other CLI dispatchers.
        """
        from boxman.providers.libvirt.oci_pull import (
            format_inspect,
            inspect_oci_image,
        )
        try:
            summary = inspect_oci_image(cli_args.image_ref)
            print(format_inspect(summary), end="", flush=True)
        except (ValueError, RuntimeError) as exc:
            print(f"error inspecting image: {exc}", flush=True)
            raise SystemExit(1) from exc

    def pxe_boot(self, cli_args):
        """
        Set boot order to network-first on a VM, start it, optionally wait
        for SSH, and optionally restore the boot order afterwards.

        Designed to be used with a Cobbler PXE provisioning server.
        """
        session = self.provider  # Phase 1 (#49): single-VM PXE flow stays on the default session until Phase 3
        vm_name = cli_args.vm

        self.logger.info(f"setting boot order to [network, hd] for '{vm_name}'")
        if not session.set_boot_order(vm_name, ['network', 'hd']):
            self.logger.error(f"failed to set boot order for '{vm_name}'")
            return False

        self.logger.info(f"starting VM '{vm_name}'")
        if not session.start_vm(vm_name):
            self.logger.error(f"failed to start VM '{vm_name}'")
            return False

        if cli_args.expected_ip:
            ok = session.wait_for_ssh(
                cli_args.expected_ip,
                timeout=cli_args.wait_timeout,
            )
            if not ok:
                self.logger.error(
                    f"SSH timeout waiting for '{vm_name}' at "
                    f"{cli_args.expected_ip}")
                return False

            if cli_args.restore_after:
                self.logger.info(
                    f"restoring boot order to [hd] for '{vm_name}'")
                session.restore_boot_order(vm_name)

        return True

    def create_templates(self, cli_args) -> None:
        """
        Create template VMs from cloud images using cloud-init.

        Reads the ``templates`` section from the project config and creates
        each template VM that doesn't already exist.

        :param cli_args: The parsed arguments from the cli
        """
        requested = None
        if cli_args is not None and hasattr(cli_args, 'template_names') and cli_args.template_names:
            requested = [t.strip() for t in cli_args.template_names.split(',')]

        force = getattr(cli_args, 'force', False) if cli_args is not None else False

        failed = self._create_templates_impl(requested=requested, force=force)
        if failed:
            self.logger.error(
                f"{len(failed)} template(s) could not be created: "
                f"{', '.join(failed)}")
            raise SystemExit(1)

    def _create_templates_impl(self, requested=None, force=False) -> list[str]:
        """
        Internal implementation for creating template VMs.

        Returns:
            The keys of the templates that could not be built. Empty when they
            all succeeded. A caller that ignores this will happily clone from a
            template whose cloud-init never finished.

        Args:
            requested: Optional list of template keys to create (None = all).
            force: If True, recreate existing templates.
        """
        from boxman.image_cache import ImageCache
        from boxman.providers.libvirt.cloudinit import CloudInitTemplate

        cache_conf = self.app_config.get('cache', {}) if self.app_config else {}
        image_cache = ImageCache.from_config(cache_conf)

        config = self.config
        templates = config.get('templates', {})

        if not templates:
            self.logger.warning("no templates defined in configuration")
            return []

        #: keys of the templates whose build failed, reported to the caller
        failed: list[str] = []

        # determine provider config — templates are libvirt-only, so the
        # libvirt block is resolved explicitly (app-merged + runtime-injected)
        provider_config = self._libvirt_provider_config()

        self.logger.info(f"resolved provider config for templates: {provider_config}")

        # resolve a workdir for template artifacts
        # Templates must live in a stable location independent of any cluster
        # workdir, so that destroying or cleaning a cluster does not delete
        # the template disk images that other clusters may still reference.
        default_workdir = '~/boxman-templates'

        # --- Pre-check: detect already-existing templates ----------------
        # Build a temporary VirshCommand to query existing VMs once.
        _virsh = VirshCommand(provider_config=provider_config)
        _existing_vms: set = set()
        result = _virsh.execute("list", "--all", "--name", hide=True, warn=True)
        if result.ok:
            _existing_vms = {
                v.strip() for v in result.stdout.strip().split("\n") if v.strip()
            }

        # Identify which of the requested templates already exist
        existing_templates: list[str] = []
        templates_to_create: list[str] = []

        for tpl_key, tpl_conf in templates.items():
            if requested and tpl_key not in requested:
                continue
            tpl_name = tpl_conf.get('name', tpl_key)
            if tpl_name in _existing_vms:
                existing_templates.append(tpl_key)
            else:
                templates_to_create.append(tpl_key)

        # If any templates already exist and --force was NOT given, error out.
        if existing_templates and not force:
            names = ", ".join(
                f"'{templates[k].get('name', k)}'" for k in existing_templates
            )
            self.logger.error(
                f"the following template(s) already exist: {names}. "
                f"Use --force to delete and recreate them."
            )
            return list(existing_templates)

        # Merge both lists (existing ones will be force-recreated)
        all_keys = existing_templates + templates_to_create
        # -----------------------------------------------------------------

        for tpl_key in all_keys:
            tpl_conf = templates[tpl_key]

            tpl_name = tpl_conf.get('name', tpl_key)
            image_field = tpl_conf.get('image', '')
            if isinstance(image_field, dict):
                image_path = image_field.get('uri', '')
                image_checksum = image_field.get('checksum', None)
            else:
                image_path = image_field
                image_checksum = None

            # Common typo: 'file' instead of 'image'
            if not image_path and 'file' in tpl_conf:
                image_path = tpl_conf['file']
                self.logger.warning(
                    f"template '{tpl_key}': 'file' is not a valid key, "
                    f"did you mean 'image'? Using '{image_path}' as the image path.")

            cloudinit_userdata = tpl_conf.get('cloudinit', None)
            cloudinit_metadata = tpl_conf.get('cloudinit_metadata', None)
            cloudinit_network_config = tpl_conf.get('cloudinit_network_config', None)
            cloudinit_done_marker = tpl_conf.get('cloudinit_done_marker', None)
            cloudinit_agent_timeout = tpl_conf.get('cloudinit_agent_timeout', 300)
            cloudinit_guest_exec_timeout = tpl_conf.get('cloudinit_guest_exec_timeout', 120)
            cloudinit_done_timeout = tpl_conf.get('cloudinit_done_timeout', 120)
            cloudinit_fallback_timeout = tpl_conf.get('cloudinit_fallback_timeout', 180)
            tpl_memory = tpl_conf.get('memory', 2048)
            tpl_vcpus = tpl_conf.get('vcpus', 2)
            tpl_os_variant = tpl_conf.get('os_variant', 'generic')
            tpl_disk_format = tpl_conf.get('disk_format', 'qcow2')
            tpl_disk_size = tpl_conf.get('disk_size', None)
            tpl_network = tpl_conf.get('network', 'default')
            tpl_bridge = tpl_conf.get('bridge', None)
            tpl_workdir = tpl_conf.get('workdir', default_workdir)

            # Ensure the workdir exists and is writable by the current user.
            # Earlier steps (e.g. docker runtime) may have created it as root.
            expanded_workdir = os.path.expanduser(tpl_workdir)
            self._ensure_writable_dir(expanded_workdir)

            # Also pre-create the template subdirectory that cloudinit.py
            # will use, so it doesn't hit PermissionError.
            template_subdir = os.path.join(expanded_workdir, tpl_name)
            self._ensure_writable_dir(template_subdir)

            self.logger.info(f"creating template '{tpl_key}' -> VM name '{tpl_name}'")

            ct = CloudInitTemplate(
                template_name=tpl_name,
                image_path=image_path,
                cloudinit_userdata=cloudinit_userdata,
                cloudinit_metadata=cloudinit_metadata,
                cloudinit_network_config=cloudinit_network_config,
                cloudinit_done_marker=cloudinit_done_marker,
                cloudinit_agent_timeout=cloudinit_agent_timeout,
                cloudinit_guest_exec_timeout=cloudinit_guest_exec_timeout,
                cloudinit_done_timeout=cloudinit_done_timeout,
                cloudinit_fallback_timeout=cloudinit_fallback_timeout,
                workdir=tpl_workdir,
                provider_config=provider_config,
                memory=tpl_memory,
                vcpus=tpl_vcpus,
                os_variant=tpl_os_variant,
                disk_format=tpl_disk_format,
                disk_size=tpl_disk_size,
                network=tpl_network,
                bridge=tpl_bridge,
                image_checksum=image_checksum,
                image_cache=image_cache,
            )

            try:
                success = ct.create_template(force=force)
            except ValueError as exc:
                # a bad timeout or marker in the template block
                self.logger.error(f"template '{tpl_key}': {exc}")
                success = False

            if success:
                self.logger.info(f"template '{tpl_key}' created successfully")
            else:
                self.logger.error(f"failed to create template '{tpl_key}'")
                failed.append(tpl_key)

        return failed

    def _ensure_writable_dir(self, path: str) -> None:
        """
        Ensure *path* exists and is writable by the current user.

        If the directory was created by another user (e.g. root via docker),
        attempt to fix ownership with ``sudo chown``.  If ``sudo`` is not
        available or fails, a clear error message is logged.

        When running under a non-local runtime the directory is also
        created inside the container so that commands executed via
        ``docker exec`` can access it.

        Args:
            path: Absolute or user-expandable directory path.
        """
        path = os.path.expanduser(path)

        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
            except PermissionError:
                # parent dir may be owned by root — try sudo mkdir
                self.logger.warning(
                    f"cannot create '{path}' as current user, "
                    f"trying with sudo...")
                result = run(
                    f"sudo mkdir -p '{path}'", hide=True, warn=True)
                if not result.ok:
                    raise PermissionError(
                        f"failed to create directory '{path}' even with sudo"
                    ) from None
                # fall through to chown below

        # Two failure modes to repair here:
        #   1. The directory itself is not writable by us (e.g. created
        #      as root by docker compose mount).
        #   2. The directory IS writable but contains stale entries
        #      owned by another user (typically root, left over from a
        #      previous docker-runtime run). Tools like `genisoimage`
        #      open their output with O_TRUNC and need write access on
        #      the existing file, not just the parent dir.
        self._normalize_ownership(path)

        # When using a non-local runtime (e.g. docker-compose), also
        # create the directory inside the container so that commands
        # executed via 'docker exec' can write to it.
        if self._runtime_name != 'local':
            mkdir_cmd = self.runtime_instance.wrap_command(
                f"mkdir -p '{path}'"
            )
            self.logger.info(f"creating directory inside runtime container: {path}")
            result = run(mkdir_cmd, hide=True, warn=True)
            if not result.ok:
                self.logger.warning(
                    f"failed to create '{path}' inside container: "
                    f"{result.stderr.strip()}")

        # Mark the directory as owned by the current runtime so that a
        # later cross-runtime reuse triggers the collision prompt.
        self._write_runtime_sentinel(path, self._runtime_name)

    def _normalize_ownership(self, path: str) -> None:
        """
        Make sure *path* and its top-level entries are usable by the
        current user.

        Strategy (cheapest path first, sudo last resort):

        1. If the directory itself is not writable by us, escalate
           straight to ``sudo chown -R``.
        2. Else, scan the top-level entries; if any are owned by
           another user, try to remove them (``unlink`` for files,
           ``shutil.rmtree`` for dirs). This works WITHOUT sudo as
           long as the parent dir is user-writable, because ``unlink``
           only needs write+exec on the parent, not on the file.
        3. If unlink/rmtree fails (e.g. nested foreign-owned tree we
           can't traverse), fall back to ``sudo chown -R``.
        4. If sudo also fails, raise ``PermissionError`` with a
           copy-pasteable fix command.

        Foreign-owned files inside boxman workdirs are always either
        stale build artifacts (seed.iso, qcow2 disk images) or stale
        provisioning files we are about to regenerate, so removing
        them is safe.
        """
        my_uid = os.getuid()
        my_gid = os.getgid()

        # Fast path: directory itself isn't writable → straight to sudo.
        dir_writable = os.access(path, os.W_OK)

        foreign_entries: list = []
        if dir_writable:
            try:
                for entry in os.scandir(path):
                    try:
                        if entry.stat(
                            follow_symlinks=False
                        ).st_uid != my_uid:
                            foreign_entries.append(entry)
                    except OSError:
                        # Can't stat → treat as foreign so we attempt
                        # the recovery path.
                        foreign_entries.append(entry)
            except OSError:
                # Can't scandir → fall through to sudo chown.
                dir_writable = False

        if dir_writable and not foreign_entries:
            return

        # Try the cheap path first: just unlink/rmtree the foreign
        # entries. No sudo needed when the parent dir is writable.
        unrecoverable: list = []
        if dir_writable and foreign_entries:
            for entry in foreign_entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path)
                    else:
                        os.unlink(entry.path)
                    self.logger.info(
                        f"removed stale foreign-owned entry: "
                        f"{entry.path}")
                except OSError as exc:
                    unrecoverable.append((entry.path, exc))

            if not unrecoverable:
                return

        # Fall back to sudo chown -R.
        self.logger.info(
            f"fixing ownership of '{path}' to {my_uid}:{my_gid} "
            f"via sudo chown -R "
            f"(directory not writable or contained foreign entries "
            f"that could not be removed)"
        )
        result = run(
            f"sudo -n chown -R {my_uid}:{my_gid} '{path}'",
            hide=True, warn=True,
        )
        if result.ok:
            return

        # Sudo failed — emit an actionable error so the user knows
        # exactly what to fix.
        offenders = "\n  ".join(
            sorted(set(
                [path]
                + [p for p, _ in unrecoverable]
                + [e.path for e in foreign_entries]
            ))
        )
        stderr = (result.stderr or "").strip()
        raise PermissionError(
            f"could not normalise ownership of '{path}'.\n"
            f"sudo chown failed: {stderr or '(no stderr)'}\n"
            f"\n"
            f"Run one of these to fix manually:\n"
            f"  sudo chown -R {my_uid}:{my_gid} '{path}'\n"
            f"  sudo rm -rf '{path}'   "
            f"# safe — boxman will recreate it\n"
            f"\n"
            f"Affected paths:\n  {offenders}"
        )

    @staticmethod
    def _is_diskless_boot(vm_info: dict) -> bool:
        """True if a VM boots from network (PXE) or cdrom (ISO).

        Such VMs are created directly via ``virt-install`` and need no
        ``base_image`` to clone from.
        """
        boot_order = vm_info.get('boot_order', ['hd'])
        return bool(boot_order) and boot_order[0] in ('network', 'cdrom')

    @staticmethod
    def _validate_cdrom_boot(vm_info: dict, iso_names: set) -> tuple[bool, str]:
        """Validate an ISO-boot VM's ``cdroms:`` up front.

        Returns ``(ok, reason)``. An ISO-boot VM must declare a first cdrom that
        either references a known ``isos:`` entry or carries an explicit local
        ``source:``.
        """
        cdroms = vm_info.get('cdroms')
        if not cdroms:
            return False, "boot_order starts with 'cdrom' but no 'cdroms:' are defined"
        first = cdroms[0]
        if isinstance(first, str):
            name = first
        elif isinstance(first, dict) and first.get('name'):
            name = first['name']
        elif isinstance(first, dict) and first.get('source'):
            return True, ""  # explicit local source, nothing to resolve
        else:
            return False, f"first cdroms entry {first!r} has neither a name nor a source"
        if name not in iso_names:
            return False, f"cdroms references unknown iso '{name}' (declare it under isos:)"
        return True, ""

    def validate_base_images(self) -> None:
        """
        Validate VM boot configuration up front, before any parallel cloning.

        - ``hd``-boot VMs (the default) must have a ``base_image`` — from the VM
          or inherited from the cluster.
        - ``cdrom``-boot (ISO) VMs must declare a valid ``cdroms:`` entry
          referencing a known ``isos:`` entry (or an explicit ``source:``).
        - ``network``-boot (PXE) VMs need neither.

        Raises ``ValueError`` aggregating every problem found.
        """
        iso_names = set((self.config.get('isos') or {}).keys())
        missing = []
        invalid = []
        for cluster_name, cluster in self.config.get('clusters', {}).items():
            cluster_base = cluster.get('base_image', '')
            for vm_name, vm_info in cluster.get('vms', {}).items():
                loc = f"{cluster_name}.vms.{vm_name}"
                boot_order = vm_info.get('boot_order', ['hd'])
                first_boot = boot_order[0] if boot_order else 'hd'
                if first_boot == 'cdrom':
                    ok, reason = self._validate_cdrom_boot(vm_info, iso_names)
                    if not ok:
                        invalid.append(f"{loc} ({reason})")
                    continue
                if first_boot == 'network':
                    continue  # PXE boot needs no base_image
                if not vm_info.get('base_image') and not cluster_base:
                    missing.append(loc)
        errors = []
        if missing:
            errors.append(
                "the following VM(s) have no base_image (set it at the cluster "
                f"or VM level): {', '.join(missing)}")
        if invalid:
            errors.append(
                "the following ISO-boot VM(s) have an invalid 'cdroms:' "
                f"configuration: {', '.join(invalid)}")
        if errors:
            raise ValueError("; ".join(errors))

    @staticmethod
    def _oci_template_name(image_ref: str) -> str:
        """Derive a deterministic, libvirt-safe template VM name from an OCI ref.

        e.g. ``oci://registry.example.com/boxman/ubuntu-24.04:latest`` ->
        ``boxman-oci-ubuntu-24.04-latest-<8 hex>``. The hash keeps distinct refs
        that share a tag/name from colliding.
        """
        from boxman.providers.libvirt.oci_pull import _strip_scheme
        ref = _strip_scheme(image_ref)
        digest = hashlib.sha256(ref.encode('utf-8')).hexdigest()[:8]
        readable = ref.rsplit('/', 1)[-1]  # e.g. 'ubuntu-24.04:latest'
        safe = "".join(c if (c.isalnum() or c in '._-') else '-' for c in readable)
        safe = safe.strip('-.') or 'image'
        return f"boxman-oci-{safe}-{digest}"

    def _expand_oci_base_images(self) -> None:
        """Expand ``base_image: oci://…`` references into implicit templates.

        The clone path can only clone an existing libvirt VM by name, so a direct
        ``oci://`` base image cannot be cloned as-is. For each unique OCI ref found
        in a cluster- or VM-level ``base_image``, synthesize a ``templates`` entry
        that pulls the qcow2 from the registry (via the ``oci://`` template-image
        path) and rewrite the ``base_image`` to that template's name. The existing
        :meth:`ensure_templates_exist` + clone pipeline then handles the rest.

        Idempotent: repeated refs map to the same template; non-OCI base images
        are left untouched. Templates synthesized here specify no explicit
        cloud-init, so the template build applies boxman's DEFAULT cloud-init
        (default user, networking) — the pulled image should therefore be a
        cloud-init-enabled cloud image. Use an explicit ``templates`` entry with
        ``image.uri: oci://…`` when you need custom cloud-init.
        """
        clusters = self.config.get('clusters', {})
        if not clusters:
            return

        templates = self.config.setdefault('templates', {})
        ref_to_name: dict[str, str] = {}

        def _resolve(base_image):
            if not isinstance(base_image, str) or not base_image.startswith('oci://'):
                return base_image
            tpl_name = ref_to_name.get(base_image)
            if tpl_name is None:
                tpl_name = self._oci_template_name(base_image)
                ref_to_name[base_image] = tpl_name
                if tpl_name not in templates:
                    templates[tpl_name] = {
                        'name': tpl_name,
                        'image': {'uri': base_image},
                    }
                    self.logger.info(
                        f"expanded base_image '{base_image}' -> "
                        f"implicit template '{tpl_name}'")
            return tpl_name

        for cluster in clusters.values():
            if 'base_image' in cluster:
                cluster['base_image'] = _resolve(cluster.get('base_image'))
            for vm_info in cluster.get('vms', {}).values():
                if 'base_image' in vm_info:
                    vm_info['base_image'] = _resolve(vm_info.get('base_image'))

    def _download_iso(self, url: str, dst_path: str) -> bool:
        """Download an ISO from a URL, trying wget then curl then urllib.

        Thin wrapper over :func:`boxman.utils.http_download.download_url`
        (which shell-quotes URL/destination and uses ``curl --fail`` so an
        HTTP 4xx/5xx error page is not accepted as a valid ISO).
        """
        self.logger.info(f"downloading ISO {url} -> {dst_path}")
        if download_url(url, dst_path):
            return True
        self.logger.error(f"failed to download ISO from {url}")
        return False

    @staticmethod
    def _iso_cache_filename(name: str, uri: str) -> str:
        """Collision-free cache filename for an ISO.

        Factory ISOs frequently share a basename (e.g. ``metal-amd64.iso``),
        which would collide in the basename-keyed cache; disambiguate with the
        declared iso name plus a short hash of the URI.
        """
        base = os.path.basename(urlparse(uri).path)
        ext = os.path.splitext(base)[1] or ".iso"
        safe = "".join(
            c if (c.isalnum() or c in "._-") else "-" for c in str(name)
        ).strip("-.") or "iso"
        digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:8]
        return f"{safe}-{digest}{ext}"

    def _resolve_isos(self) -> dict[str, str]:
        """Download and cache all ISOs declared in the ``isos:`` config section.

        Returns a mapping of iso_name -> local_file_path.
        """
        isos_conf = self.config.get("isos", {}) if self.config else {}
        if not isos_conf:
            return {}
        if not isinstance(isos_conf, dict):
            raise ValueError(
                "'isos:' must be a mapping of <name>: {uri: ..., checksum: ...}, "
                f"got {type(isos_conf).__name__}")

        # ISO boot needs the file visible to the in-container virt-install; the
        # host cache dir is not bind-mounted under a containerized runtime. Fail
        # fast with guidance instead of a confusing missing-file error later.
        runtime_name = getattr(self, "_runtime_name", "local") or "local"
        if runtime_name != "local":
            raise RuntimeError(
                f"ISO boot ('isos:') is not yet supported under the "
                f"'{runtime_name}' runtime; use the local runtime "
                f"(the downloaded ISO is not visible inside the libvirt container)."
            )

        cache_conf = (self.app_config or {}).get("cache", {})
        cache = ImageCache.from_config(cache_conf)

        resolved: dict[str, str] = {}
        for name, iso_conf in isos_conf.items():
            if not isinstance(iso_conf, dict):
                raise ValueError(
                    f"iso '{name}' must be a mapping with at least a 'uri' key"
                )
            uri = iso_conf.get("uri")
            if not uri:
                raise ValueError(f"iso '{name}' missing 'uri'")
            checksum = iso_conf.get("checksum")
            filename = self._iso_cache_filename(name, uri)

            local_path = cache.ensure(uri, self._download_iso, filename=filename)
            if local_path is None:
                if not cache.enabled:
                    # Caching disabled: download directly to a stable path so the
                    # ISO is still available to virt-install (re-downloaded each
                    # run). Mirrors the base-image direct-download fallback.
                    local_path = cache.cache_path_for(uri, filename=filename)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    if not self._download_iso(uri, local_path):
                        raise RuntimeError(
                            f"Failed to download ISO '{name}' from {uri}")
                else:
                    raise RuntimeError(
                        f"Failed to download ISO '{name}' from {uri}")

            if checksum and not ImageCache.verify_checksum(local_path, checksum):
                # Evict the bad file so a later run re-downloads instead of
                # re-failing forever against the poisoned cache entry.
                try:
                    os.remove(local_path)
                except OSError:
                    pass
                raise RuntimeError(f"Checksum mismatch for ISO '{name}'")

            resolved[name] = local_path

        return resolved

    def _inject_resolved_iso(
        self, vm_info: dict, resolved_isos: dict[str, str]
    ) -> dict:
        """Resolve ``cdroms:`` name references and inject ``_resolved_iso_path``.

        Returns a shallow copy of vm_info with:
        - Each cdrom entry expanded to carry ``source: <local_path>``. Entries
          may be a plain string (an iso name), a ``{name: <iso>}`` mapping, or a
          ``{source: <local path>}`` mapping; anything else is a clear error.
        - ``_resolved_iso_path`` set to ``cdroms[0]['source']`` when
          ``boot_order[0] == 'cdrom'``
        """
        cdroms = vm_info.get("cdroms", [])
        if not cdroms:
            return {**vm_info}

        def _resolve_name(iso_name: str) -> str:
            if iso_name not in resolved_isos:
                raise ValueError(
                    f"cdroms references unknown iso '{iso_name}'. "
                    f"Declare it in the 'isos:' section."
                )
            return resolved_isos[iso_name]

        resolved_cdroms = []
        for cdrom in cdroms:
            if isinstance(cdrom, str):
                resolved_cdroms.append(
                    {"name": cdrom, "source": _resolve_name(cdrom)})
            elif isinstance(cdrom, dict) and cdrom.get("name"):
                resolved_cdroms.append(
                    {**cdrom, "source": _resolve_name(cdrom["name"])})
            elif isinstance(cdrom, dict) and cdrom.get("source"):
                resolved_cdroms.append(cdrom)
            else:
                raise ValueError(
                    f"invalid cdroms entry {cdrom!r}: expected a string iso "
                    f"name, a {{name: <iso>}} mapping, or a {{source: <path>}} "
                    f"mapping"
                )

        result = {**vm_info, "cdroms": resolved_cdroms}

        boot_order = vm_info.get("boot_order", ["hd"])
        if boot_order and boot_order[0] == "cdrom" and resolved_cdroms:
            first_source = resolved_cdroms[0].get("source")
            if first_source:
                result["_resolved_iso_path"] = first_source

        return result

    def _resolved_network_names(self, cluster_name: str, vm_info: dict) -> list[str]:
        """Fully-qualified libvirt names for a VM's ``networks:`` (first-NIC list).

        Direct-boot VMs (ISO/PXE) attach networks at ``virt-install`` time, so
        the raw cluster-network name must be namespaced exactly as cluster
        networks are defined (``bprj__…__clstr__…__<name>``).
        """
        names = []
        for net in vm_info.get("networks") or []:
            if isinstance(net, dict) and net.get("name"):
                names.append(self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=net["name"],
                ))
        return names

    def _resolve_iso_config(self) -> None:
        """Resolve ISO/cdrom/network references for direct-boot VMs, in place.

        Downloads+caches declared ``isos:``, expands each VM's ``cdroms:`` to
        local sources, sets ``_resolved_iso_path`` for cdrom-boot VMs, and
        namespaces ``networks:`` into ``_resolved_networks``. Mutating
        ``self.config`` means both the clone subprocesses and the later
        configure/start step observe the resolved values (otherwise the resolved
        ISO source never reaches the CDROM-attach path). Idempotent.
        """
        clusters = self.config.get("clusters", {})
        if not clusters:
            return
        resolved_isos = self._resolve_isos()
        for cluster_name, cluster in clusters.items():
            for vm_name, vm_info in cluster.get("vms", {}).items():
                resolved = self._inject_resolved_iso(vm_info, resolved_isos)
                if self._is_diskless_boot(resolved):
                    resolved["_resolved_networks"] = self._resolved_network_names(
                        cluster_name, resolved)
                cluster["vms"][vm_name] = resolved

    def ensure_templates_exist(self) -> bool:
        """
        Check if any cluster's base_image refers to a template defined in the
        ``templates`` section of the config. If the template VM does not exist,
        create it automatically.

        Returns:
            True if all required templates exist (or were created), False on failure.
        """
        templates = self.config.get('templates', {})
        if not templates:
            return True  # nothing to do

        # build a mapping: template VM name -> template key
        tpl_name_to_key: dict[str, str] = {}
        for tpl_key, tpl_conf in templates.items():
            vm_name = tpl_conf.get('name', tpl_key)
            tpl_name_to_key[vm_name] = tpl_key
            # also map by the key itself in case base_image uses the key
            tpl_name_to_key[tpl_key] = tpl_key

        # collect which templates are referenced by clusters or individual VMs
        needed_template_keys: set = set()
        for _cluster_name, cluster in self.config.get('clusters', {}).items():
            base_image = cluster.get('base_image', '')
            if base_image in tpl_name_to_key:
                needed_template_keys.add(tpl_name_to_key[base_image])
            for _vm_name, vm_info in cluster.get('vms', {}).items():
                vm_base = vm_info.get('base_image', '')
                if vm_base and vm_base in tpl_name_to_key:
                    needed_template_keys.add(tpl_name_to_key[vm_base])

        if not needed_template_keys:
            return True  # no cluster uses a template as base_image

        # check which of the needed templates already exist as VMs
        missing_keys: list = []      # template VM not registered at all
        broken_keys: list = []       # registered, but backing disk gone
        for tpl_key in needed_template_keys:
            tpl_conf = templates[tpl_key]
            tpl_vm_name = tpl_conf.get('name', tpl_key)

            # ask the provider if the VM exists
            # Phase 1 (#49): template management stays on the default
            # session — templates are project-level and libvirt-only.
            exists = False
            if self.provider is not None and hasattr(self.provider, 'vm_exists'):
                exists = self.provider.vm_exists(tpl_vm_name)
            elif self.provider is not None:
                # fallback: try virsh list
                try:
                    result = self._virsh().execute("list", "--all", "--name", hide=True, warn=True)
                    if result.ok:
                        vm_list = [v.strip() for v in result.stdout.strip().split("\n") if v.strip()]
                        exists = tpl_vm_name in vm_list
                except Exception as exc:
                    self.logger.warning(f"could not check if template VM '{tpl_vm_name}' exists: {exc}")

            if exists:
                # The domain is registered, but its backing qcow2 file may
                # have been deleted out from under libvirt. virt-clone then
                # fails inscrutably during provision; detect it up-front and
                # rebuild via the existing force-recreate path which already
                # does destroy + undefine --remove-all-storage on its own.
                disks_ok = True
                if (self.provider is not None
                        and hasattr(self.provider, 'template_disks_present')):
                    try:
                        disks_ok = self.provider.template_disks_present(tpl_vm_name)
                    except Exception as exc:
                        self.logger.warning(
                            f"could not verify template '{tpl_vm_name}' disks: {exc}")
                if not disks_ok:
                    self.logger.warning(
                        f"template VM '{tpl_vm_name}' is registered with libvirt "
                        f"but its disk file is missing on the host — rebuilding "
                        f"the template (force=True will destroy the orphan domain)")
                    broken_keys.append(tpl_key)
                else:
                    self.logger.info(
                        f"template VM '{tpl_vm_name}' already exists, skipping creation")
            else:
                self.logger.info(
                    f"template VM '{tpl_vm_name}' (key='{tpl_key}') does not exist, "
                    f"will create it before provisioning")
                missing_keys.append(tpl_key)

        if not missing_keys and not broken_keys:
            return True

        failed: list[str] = []
        if missing_keys:
            self.logger.info(
                f"auto-creating {len(missing_keys)} missing template(s): {missing_keys}")
            failed += self._create_templates_impl(
                requested=missing_keys, force=False)
        if broken_keys:
            self.logger.info(
                f"auto-rebuilding {len(broken_keys)} broken template(s): {broken_keys}")
            failed += self._create_templates_impl(
                requested=broken_keys, force=True)

        if failed:
            # returning True here would let provisioning carry on and clone
            # from a template whose cloud-init never finished
            self.logger.error(
                f"template(s) {', '.join(failed)} could not be built, so the "
                f"VMs that use them cannot be cloned")
            return False

        return True
