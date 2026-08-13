# Host Memory Deduplication with KSM

Kernel Samepage Merging (KSM) can reduce the physical memory used by a
fleet of similar KVM guests. It is a Linux host policy, not a Boxman VM
setting: Boxman neither enables KSM nor manages `ksmd`/`ksmtuned`.

Use KSM as a complement to, not a replacement for, virtio-balloon
free-page reporting:

| Mechanism | Reclaims | Scope | Boxman surface |
|---|---|---|---|
| Free-page reporting | Pages the guest has freed | One VM | `memballoon.free_page_reporting: true` |
| KSM | Identical live anonymous pages in mergeable host processes | The whole host | Operator-managed host policy |

Free-page reporting returns unused guest pages promptly. KSM scans live
pages which QEMU has marked mergeable, replaces identical pages with one
write-protected copy, and copies a page again when a guest writes to it.
The mechanisms can therefore save different parts of the same VM's
memory footprint.

The [Linux kernel KSM documentation][kernel-ksm] defines the controls,
counters, and memory-profit calculation used below.

## Decide at the host level

KSM affects every mergeable process on the host, including VMs outside
the Boxman project being evaluated. Enabling it is an operator decision.
Before changing anything, record:

```bash
getconf PAGE_SIZE
grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
for value in /sys/kernel/mm/ksm/*; do
    printf '%s=' "${value##*/}"
    cat "$value"
done
systemctl is-enabled ksmtuned.service ksm.service 2>/dev/null || true
systemctl is-active ksmtuned.service ksm.service 2>/dev/null || true
ps -C ksmd -o pid,etime,time,%cpu,cmd
```

Service and package names vary by distribution. If the distribution
provides `ksmtuned`, prefer its adaptive policy over an undocumented
collection of persistent sysfs writes. Do not run `ksmtuned` and a
manual policy at the same time: the service may replace the manual
settings while an experiment is running.

Consider these trade-offs before opting in:

- **CPU and latency:** `ksmd` hashes and compares candidate pages.
  Aggressive scanning can consume a substantial fraction of one CPU and
  add copy-on-write latency when a guest modifies a merged page.
- **Temporary memory cost:** KSM allocates reverse-map metadata before it
  finds profitable sharing. `general_profit` can be negative during an
  early scan even if later scans save memory.
- **Unmerge headroom:** setting `run=2` materializes private copies of
  merged pages. Ensure enough `MemAvailable` exists first; unmerging
  without headroom can invoke the OOM killer.
- **NUMA placement:** `merge_across_nodes=1` may save more memory but can
  place a shared page away from some consumers. Measure workload latency
  before enabling cross-node merging on a NUMA host.
- **Trust boundaries:** sharing pages across mutually untrusted tenants
  can create timing side channels. Copy-on-write and memory-pressure
  behavior can also become coupled across workloads. Avoid host-wide
  KSM across trust domains without a security assessment.

## Run a bounded evaluation

Use an idle maintenance window and a representative, stable fleet. Keep
the guest workload, VM count, free-page-reporting setting, and all other
memory controls fixed between the KSM-off and KSM-on measurements.

1. With `run=0`, let the workload stabilize and capture the baseline
   measurements described below.
2. Choose a conservative scan rate. `pages_to_scan` is the number of
   pages processed before `ksmd` sleeps for `sleep_millisecs`; their
   combination determines the upper scan rate. There is no universally
   safe value. Start low and monitor CPU, `MemAvailable`, and
   `general_profit`.
3. Start KSM and wait for a scan-count target rather than an arbitrary
   delay. At least two increments of `full_scans` let pages initially
   classified as changing be examined again.
4. Stop the scanner with `run=0` before the final sample. This freezes
   scanning but retains already merged pages, making repeated samples
   easier to compare.

The manual control surface is:

```bash
# Optional: explicitly set this to the bounded batch size selected for this host.
printf '%s\n' "$PAGES_PER_BATCH" | sudo tee /sys/kernel/mm/ksm/pages_to_scan

# Start scanning.
echo 1 | sudo tee /sys/kernel/mm/ksm/run

# Stop scanning but retain merged pages for measurement.
echo 0 | sudo tee /sys/kernel/mm/ksm/run
```

Monitor at a fixed interval and set a wall-time limit. Stop early if
`MemAvailable` approaches the host's safety reserve, `ksmd` CPU is too
high, or profit remains negative. Do not copy a scan rate from another
machine without measuring it locally.

## Measure savings and cost

### Host-global counters

```bash
KSM=/sys/kernel/mm/ksm
page_size=$(getconf PAGE_SIZE)
pages_sharing=$(cat "$KSM/pages_sharing")

awk -v sharing="$pages_sharing" -v size="$page_size" \
    'BEGIN { printf "Ordinary KSM savings: %.1f MiB\n", \
                    sharing * size / 1048576 }'

if test -r "$KSM/ksm_zero_pages"; then
    zero_pages=$(cat "$KSM/ksm_zero_pages")
    awk -v sharing="$pages_sharing" -v zero="$zero_pages" \
        -v size="$page_size" \
        'BEGIN { printf "Savings including zero pages: %.1f MiB\n", \
                        (sharing + zero) * size / 1048576 }'
else
    echo 'Savings including zero pages: N/A (counter unavailable)'
fi

cat "$KSM/general_profit"
cat "$KSM/full_scans"
cat "$KSM/pages_shared"
cat "$KSM/pages_unshared"
cat "$KSM/pages_volatile"
ps -C ksmd -o pid,etime,time,%cpu,cmd
```

The important distinctions are:

- `pages_sharing` is the number of additional mappings sharing KSM
  pages: multiply it by `PAGE_SIZE` to estimate bytes saved globally.
  If `use_zero_pages` is or was enabled, add `ksm_zero_pages` before
  multiplying to include mappings deduplicated against the kernel zero
  page. Older kernels may not expose that counter; report zero-page-
  inclusive savings as unavailable rather than assuming it is zero.
- `pages_shared` is the number of shared KSM pages retained, not the
  number of pages saved.
- `general_profit` is the kernel's system-wide estimate after KSM
  metadata cost. A negative value means the current policy costs more
  memory than it saves.
- `full_scans` counts completed passes over all registered mergeable
  areas. Record elapsed time and the change in `ksmd` CPU time across
  those passes.

These counters are host-global. They cannot attribute savings to one
Boxman project when other mergeable workloads are present.

### Project and process attribution

First identify the QEMU PIDs belonging to the target domains. For each
PID, record proportional set size (PSS) and KSM statistics:

```bash
sudo awk '/^(Rss|Pss):/' /proc/<qemu-pid>/smaps_rollup
sudo cat /proc/<qemu-pid>/ksm_stat
```

Sum PSS across the same target PIDs before and after KSM. RSS is not a
useful KSM savings measure by itself because a shared page is still
resident in every process which maps it. On kernels which expose
`ksm_stat`:

- `ksm_merging_pages + ksm_zero_pages`, multiplied by `PAGE_SIZE`,
  estimates saved mappings for that process;
- `ksm_process_profit` reports its estimated benefit after KSM metadata.

Not every supported distribution kernel exposes `/proc/<pid>/ksm_stat`.
Use aggregate PSS as the project-level measurement when it is absent.

## Controlled Boxman fleet result

A controlled evaluation used six powered-on, same-template 1 GiB Linux
VMs. Every guest ran the same transient memory workload, free-page
reporting remained enabled in both arms, and KSM was the only intended
independent variable. Measurements were taken after at least two full
scans and repeated with scanning frozen.

- Aggregate QEMU PSS fell from about 3.29 GiB to 1.31 GiB: a **60.3%**
  reduction, or about **1.98 GiB**.
- Per-process KSM statistics attributed about **2.09 GiB** of merged
  mappings and **2.04 GiB** of net process profit to the six VMs.
- Aggregate RSS changed by only 1.3%, illustrating why PSS and
  per-process KSM counters are the meaningful attribution measures.
- The deliberately accelerated scan completed two full passes in about
  170 seconds but used roughly 68% of one CPU on average. During the
  first 30 seconds it had not merged pages yet, and host-global profit
  was negative because reverse-map metadata had already been allocated.

The result supports KSM as a useful operator option for dense,
same-template fleets. It does not establish a safe default scan policy
for other kernels, hosts, workloads, NUMA layouts, or trust models.

## Restore the original policy

Record the original values before the experiment and restore them
exactly afterward. To remove the merged state, not merely stop future
scans:

```bash
# Check headroom before unmerging.
grep '^MemAvailable:' /proc/meminfo

# Stop ksmd and unmerge all currently merged pages.
echo 2 | sudo tee /sys/kernel/mm/ksm/run

# Set these variables from the values recorded before the evaluation.
printf '%s\n' "$ORIGINAL_PAGES_TO_SCAN" | \
    sudo tee /sys/kernel/mm/ksm/pages_to_scan
printf '%s\n' "$ORIGINAL_RUN_VALUE" | sudo tee /sys/kernel/mm/ksm/run
```

Also restore the original enabled/active state of any KSM service. After
an unmerge, verify `pages_shared`, `pages_sharing`, `pages_unshared`,
`pages_volatile`, and `general_profit` returned to zero. Historical
counters such as `pages_scanned` and accumulated `ksmd` CPU time do not
reset through the normal sysfs controls.

## Why Boxman does not automate this yet

The observed project-level saving is large enough to document, but not
enough to justify an automatic host-preparation helper. A policy helper
would need to account for distribution-specific service management,
host-wide workloads, capacity reserve, kernel differences, NUMA
topology, and security boundaries. It could otherwise make an unrelated
VM slower or make unmerge unsafe.

A future first step should be a read-only readiness and measurement
command. Persistent policy enablement should remain explicit and
operator-owned until results cover more kernels, hosts, and long-running
workloads.

[kernel-ksm]: https://docs.kernel.org/admin-guide/mm/ksm.html
