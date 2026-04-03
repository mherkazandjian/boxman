#!/usr/bin/env python3
"""Generate the restore-time comparison bar chart for the tutorial."""

import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# Data: platform names and midpoint restore times in seconds (for a small 2-4GB VM)
# Sources:
#   - KVM/libvirt: Red Hat docs, libvirt wiki (internal snapshots, ~5-30s)
#   - AWS EC2 FSR: AWS blog "New EBS Fast Snapshot Restore" (~30s-2min)
#   - Azure: Microsoft Learn "Instant Restore Capability" (~1-5min)
#   - OpenStack: CERN cloud guide, Cinder specs (~2-5min)
#   - GCP: Google Cloud docs "Restore from snapshot" (~2-10min)
#   - AWS EC2 no FSR: AWS Storage Blog "Addressing I/O latency" (~5-10min)

platforms = [
    'Boxman\n(KVM/libvirt)',
    'AWS EC2\n(Fast Snapshot\nRestore)',
    'Azure\nInstant Restore',
    'OpenStack\nSnapshot',
    'GCP\nInstant Snapshot',
    'AWS EC2\n(standard)',
]

# Midpoint of estimated ranges, in seconds
times = [15, 75, 180, 210, 360, 450]

# Color gradient: green for fast, orange/red for slow
colors = ['#27ae60', '#f39c12', '#e67e22', '#d35400', '#c0392b', '#8e44ad']

fig, ax = plt.subplots(figsize=(11, 5))

bars = ax.barh(
    range(len(platforms)), times,
    color=colors, edgecolor='white', height=0.65,
    zorder=3
)

ax.set_yticks(range(len(platforms)))
ax.set_yticklabels(platforms, fontsize=11, fontfamily='sans-serif')

# Add time labels on bars
for i, (bar, t) in enumerate(zip(bars, times)):
    if t < 60:
        label = f'~{t}s'
    elif t < 120:
        label = f'~{t/60:.1f} min'
    else:
        label = f'~{t/60:.0f} min'
    ax.text(
        bar.get_width() + 12, bar.get_y() + bar.get_height() / 2,
        label, va='center', fontsize=11, fontweight='bold', color='#2c3e50'
    )

ax.set_xlabel('Restore Time (seconds) — lower is better', fontsize=12, color='#2c3e50')
ax.set_title(
    'VM Snapshot Restore Time Comparison (small VM, 2-4 GB)',
    fontsize=14, fontweight='bold', color='#2c3e50', pad=15
)
ax.set_xlim(0, 560)
ax.invert_yaxis()

# Style
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.spines['left'].set_visible(False)
ax.tick_params(left=False)
ax.grid(axis='x', alpha=0.3, zorder=0)

plt.tight_layout()

# Save SVG
script_dir = os.path.dirname(os.path.abspath(__file__))
assets_dir = os.path.join(script_dir, '..', 'assets')
os.makedirs(assets_dir, exist_ok=True)
output_path = os.path.join(assets_dir, 'restore-time-comparison.svg')
fig.savefig(output_path, format='svg', bbox_inches='tight', transparent=True)
print(f'Chart saved to {output_path}')

plt.close()
