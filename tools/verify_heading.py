"""Pin Frigate's velocity_angle convention against real captures.

For every frigate/events message whose track has >= 2 path_data points and a
non-zero speed, compute the image-space movement angle from the last two path
points (atan2(dy, dx) with y pointing DOWN, standard image coords) and
compare it to the reported velocity_angle. If Frigate's angle is in the same
image space, the circular difference clusters near a constant offset.

Usage: python tools/verify_heading.py capture.jsonl [capture2.jsonl ...]
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict


def path_points(path_data):
    """Both observed shapes: [[x, y, t], ...] and [[[x, y], t], ...]."""
    pts = []
    for entry in path_data or []:
        try:
            if len(entry) == 3:
                x, y, t = entry
            elif len(entry) == 2 and isinstance(entry[0], (list, tuple)):
                (x, y), t = entry
            else:
                continue
            pts.append((float(x), float(y), float(t)))
        except (TypeError, ValueError):
            continue
    return pts


def circular_diff(a: float, b: float) -> float:
    return ((a - b) + 180.0) % 360.0 - 180.0


def main(paths: list[str]) -> None:
    diffs_by_camera: dict[str, list[float]] = defaultdict(list)
    samples = 0
    for path in paths:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not row.get("topic", "").endswith("events"):
                    continue
                after = (row.get("payload") or {}).get("after") or {}
                angle = after.get("velocity_angle")
                speed = after.get("average_estimated_speed")
                if angle is None or not speed:
                    continue
                pts = path_points(after.get("path_data"))
                if len(pts) < 2:
                    continue
                (x0, y0, _), (x1, y1, _) = pts[-2], pts[-1]
                dx, dy = x1 - x0, y1 - y0
                if abs(dx) < 1e-4 and abs(dy) < 1e-4:
                    continue
                # Image coords, y down.
                path_angle = math.degrees(math.atan2(dy, dx)) % 360.0
                diffs_by_camera[after.get("camera", "?")].append(
                    circular_diff(path_angle, float(angle) % 360.0)
                )
                samples += 1

    print(f"samples: {samples}")
    for camera, diffs in sorted(diffs_by_camera.items()):
        diffs.sort()
        n = len(diffs)
        median = diffs[n // 2]
        # Circular mean for a sanity cross-check.
        s = sum(math.sin(math.radians(d)) for d in diffs)
        c = sum(math.cos(math.radians(d)) for d in diffs)
        mean = math.degrees(math.atan2(s, c))
        within30 = sum(1 for d in diffs if abs(circular_diff(d, mean)) <= 30) / n
        print(
            f"{camera:16s} n={n:5d} median_diff={median:7.1f} "
            f"circ_mean={mean:7.1f} within±30°={within30:.0%}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
