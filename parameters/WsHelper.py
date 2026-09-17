import math, json
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle

from floe import Parameter
from floe import make_var
import json


# --- Die Slicing Scheme - module DEFAULTS ---
# These are the fallback values used when no config Variable is wired. A wired
# JSON `config` (and optional `ignore_reticles`) Variable overrides them per
# canvas, so a 'run' is a config, not a forked file. See README.md for an
# example blob. Units are mm; columns = die widths, rows = die heights.
DIE_WIDTHS: list[float] = [4, 4, 4, 4, 4, 4, 4, 2, 2]
DIE_HEIGHTS: list[float] = list(reversed([2.6, 2.6, 5.2, 5.2, 5.2, 5.2]))
KERF: float = 0           # Saw kerf in mm
WAFER_SIZE: float = 200   # Wafer size in mm
EXLUSION_ZONE: float = 3.0    # Exclusion zone from wafer edge in mm
CENTER_OFFSET_X: float = 0.0  # Reticle grid offset from wafer center in mm
CENTER_OFFSET_Y: float = 0.0

IGNORE_FIRST_DIE_COL = False
IGNORE_LAST_DIE_COL = True
IGNORE_FIRST_DIE_ROW = False
IGNORE_LAST_DIE_ROW = True

ROTATION = 0  # whole-wafer rotation in degrees: 0, 90, or -90 (right angles only)

# Single source of fallback config. compute_die_yield() merges a wired config
# over this dict, so a canvas can override only the keys it cares about.
DEFAULTS = {
    "die_widths": DIE_WIDTHS,
    "die_heights": DIE_HEIGHTS,
    "kerf": KERF,
    "wafer_size": WAFER_SIZE,
    "exclusion_zone": EXLUSION_ZONE,
    "center_offset_x": CENTER_OFFSET_X,
    "center_offset_y": CENTER_OFFSET_Y,
    "rotation": ROTATION,
    "ignore_reticles": [],
    "ignore_first_die_col": IGNORE_FIRST_DIE_COL,
    "ignore_last_die_col": IGNORE_LAST_DIE_COL,
    "ignore_first_die_row": IGNORE_FIRST_DIE_ROW,
    "ignore_last_die_row": IGNORE_LAST_DIE_ROW,
}


def _rotate_point(x: float, y: float, rotation: int) -> tuple:
    """Rotate (x, y) about the wafer center (origin) by a right angle.
    Only 0 / 90 / -90 / 180 are meaningful - the wafer is only ever loaded at
    a right angle, never oblique. CCW positive (math convention)."""
    rot = rotation % 360
    if rot == 90:
        return -y, x
    if rot == 270:   # i.e. -90
        return y, -x
    if rot == 180:
        return -x, -y
    return x, y


def die_yield_advanced(
    die_widths: list[float],
    die_heights: list[float],
    kerf: float,
    wafer_size: float = 200,
    exclusion_zone: float = 3.0,
    plot: bool = False,
    heatmap: bool = False,
    filename: str = None,
    center_offset_x: float = 0.0,
    center_offset_y: float = 0.0,
    rotation: int = 0,
    ignore_reticles: set = None,
    ignore_first_die_col: bool = False,
    ignore_last_die_col: bool = True,
    ignore_first_die_row: bool = False,
    ignore_last_die_row: bool = True,
) -> tuple:
    """
    Compute usable die yield with unique shot (reticle) identifiers and SVG output.

    Returns:
        total_dies: Total count
        die_data: list of (x_center, y_center, full_label)
        reticle_yield_counts: 2D array of yield per reticle position
        reticle_grid: {(ix, iy): enabled} for every reticle overlapping the wafer
    """

    wafer_r = wafer_size / 2.0 - exclusion_zone
    reticle_cols = len(die_widths)
    reticle_rows = len(die_heights)

    px_list = [dw + kerf for dw in die_widths]
    py_list = [dh + kerf for dh in die_heights]

    ret_w = sum(px_list) - kerf
    ret_h = sum(py_list) - kerf

    n_rx = int(math.ceil((wafer_r + ret_w/2) / ret_w))
    n_ry = int(math.ceil((wafer_r + ret_h/2) / ret_h))

    if ignore_reticles is None:
        ignore_reticles = set()

    die_data = []
    reticle_yield_counts = np.zeros((reticle_rows, reticle_cols), dtype=int)
    total_dies = 0
    reticle_boundaries = []
    reticle_grid = {}  # {(ix, iy): bool} - True if on wafer, value indicates enabled

    # First pass (C3): determine which reticles overlap the wafer
    for ix in range(-n_rx, n_rx):
        for iy in range(-n_ry, n_ry):
            ox = ix * ret_w + center_offset_x
            oy = iy * ret_h + center_offset_y
            has_dies = False
            for c in range(reticle_cols):
                x_start_rel = sum(px_list[:c])
                hx = die_widths[c] / 2.0
                for r in range(reticle_rows):
                    y_start_rel = sum(py_list[:r])
                    hy = die_heights[r] / 2.0
                    ccx, ccy = ox + x_start_rel + hx, oy + y_start_rel + hy
                    corners = [
                        (ccx - hx, ccy - hy), (ccx + hx, ccy - hy),
                        (ccx + hx, ccy + hy), (ccx - hx, ccy + hy)
                    ]
                    if all(x*x + y*y <= wafer_r**2 for x, y in corners):
                        has_dies = True
                        break
                if has_dies:
                    break
            if has_dies:
                reticle_grid[(ix, iy)] = (ix, iy) not in ignore_reticles

    # Second pass: compute dies
    for ix in range(-n_rx, n_rx):
        for iy in range(-n_ry, n_ry):
            ox = ix * ret_w + center_offset_x
            oy = iy * ret_h + center_offset_y
            reticle_boundaries.append((ox, oy, (ix, iy) in ignore_reticles))

            if (ix, iy) in ignore_reticles:
                continue

            # Shot ID based on grid position
            shot_id = f"S{ix}_{iy}"

            for c in range(reticle_cols):
                x_start_rel = sum(px_list[:c])
                die_w = die_widths[c]
                hx = die_w / 2.0

                for r in range(reticle_rows):
                    y_start_rel = sum(py_list[:r])
                    die_h = die_heights[r]
                    hy = die_h / 2.0

                    cx, cy = ox + x_start_rel + hx, oy + y_start_rel + hy

                    corners = [
                        (cx - hx, cy - hy), (cx + hx, cy - hy),
                        (cx + hx, cy + hy), (cx - hx, cy + hy)
                    ]

                    if all(x*x + y*y <= wafer_r**2 for x, y in corners):
                        # Combined Label: Shot ID + Die Position
                        full_label = f"{shot_id},C{c}R{r}"
                        if c == 0 and ignore_first_die_col:
                            continue
                        if c == reticle_cols-1 and ignore_last_die_col:
                            continue
                        if r == 0 and ignore_first_die_row:
                            continue
                        if r == reticle_rows-1 and ignore_last_die_row:
                            continue
                        # Rotation about the wafer center is applied to the
                        # emitted coordinate only - containment was checked in
                        # the un-rotated frame (distance from origin is rotation
                        # invariant), and the (ix,iy)/(c,r) identity is unchanged.
                        rcx, rcy = _rotate_point(cx, cy, rotation)
                        die_data.append((rcx, rcy, full_label))
                        total_dies += 1
                        reticle_yield_counts[r, c] += 1

    if plot or filename:
        fig, ax = plt.subplots(figsize=(14, 14))
        wafer = Circle((0,0), wafer_size / 2.0, edgecolor='black', facecolor='none', lw=2)
        ax.add_patch(wafer)
        # V1: exclusion-zone ring
        exclusion = Circle((0,0), wafer_r, edgecolor='gray', facecolor='none', lw=0.5, linestyle='--', alpha=0.5)
        ax.add_patch(exclusion)

        # V4: 17-colour palette (kept colored die fill - V2 white-fill dropped)
        colors = ['red', 'blue', 'green', 'orange', 'purple', 'brown', 'pink',
                  'gray', 'olive', 'cyan', 'navy', 'teal', 'maroon', 'gold',
                  'salmon', 'indigo', 'lime']

        # Plot usable dies
        for cx, cy, label in die_data:
            # Parse label to get dimensions for drawing
            # Label format: "S[ix,iy]-CcRr"
            pos_part = label.split(',')[1]
            c_idx = int(pos_part.split('R')[0][1:])
            r_idx = int(pos_part.split('R')[1])
            w, h = die_widths[c_idx], die_heights[r_idx]
            # cx, cy are already rotated; a right-angle turn swaps the footprint.
            if rotation % 180 == 90:
                w, h = h, w

            color_idx = (c_idx * len(die_heights) + r_idx) % 17
            rect = Rectangle((cx - w/2, cy - h/2), w, h, edgecolor='black', facecolor=colors[color_idx], lw=0.4, alpha=.5)
            ax.add_patch(rect)

            # Red dot at center (V6 kept)
            ax.plot(cx, cy, 'ro', markersize=1.5)

            # Full ShotID label (V3 short-labels dropped - keep WsHelper style)
            ax.text(cx, cy, label, fontsize=2.5, ha='center', va='center', color='darkgreen', rotation=45)

        # Plot reticle boundaries (V5: grey fill for ignored ones).
        # Rotate each rectangle about its center, swapping w/h on a right angle.
        swap = rotation % 180 == 90
        for ox, oy, ignored in reticle_boundaries:
            rcx, rcy = _rotate_point(ox + ret_w/2, oy + ret_h/2, rotation)
            rw, rh = (ret_h, ret_w) if swap else (ret_w, ret_h)
            bx, by = rcx - rw/2, rcy - rh/2
            if ignored:
                rect = Rectangle((bx, by), rw, rh, edgecolor='blue', facecolor='lightgray', lw=0.8, linestyle='--', alpha=0.6)
            else:
                rect = Rectangle((bx, by), rw, rh, edgecolor='blue', facecolor='none', lw=0.8, linestyle='--', alpha=0.4)
            ax.add_patch(rect)

        ax.set_aspect('equal')
        ax.set_xlim(-wafer_r-10, wafer_r+10)
        ax.set_ylim(-wafer_r-10, wafer_r+10)
        ax.set_title(f"Wafer Map: {total_dies} usable dies\nLabel Format: ShotID-ColumnRow")

        if plot:
            if filename:
                plt.savefig(f"{filename}.svg", format='svg', bbox_inches='tight')

            # plt.show()
        else:
            plt.close()

    # Reticle yield heatmap
    if heatmap and plot:
        fig, ax = plt.subplots(figsize=(6,5))
        # Hide die positions that yielded nothing - edge-ignored columns/rows
        # (and positions that never fit on the wafer) are 0, so mask them out
        # instead of painting a "0" cell. Only meaningful yields remain.
        masked = np.ma.masked_equal(reticle_yield_counts, 0)
        cmap = plt.cm.viridis.copy()
        cmap.set_bad(color='white')
        im = ax.imshow(masked, cmap=cmap, origin="lower")
        for r in range(reticle_yield_counts.shape[0]):
            for c in range(reticle_yield_counts.shape[1]):
                v = reticle_yield_counts[r, c]
                if v == 0:
                    continue
                ax.text(c, r, str(v), ha="center", va="center", color='white', fontsize=8)
        ax.set_title("Reticle Position Yield (usable dies)")
        ax.set_xlabel("Column index")
        ax.set_ylabel("Row index")
        fig.colorbar(im, ax=ax, label="Usable dies")

        plt.savefig(f"{filename}_heatmap.svg", format='svg', bbox_inches='tight')

        # plt.show()

    with open(f"{filename}.csv", 'w') as f:
        # save CSV file for pnp
        f.write("X,Y,RETICLE_SHOT,COL|ROW\n")
        for die in die_data:
            f.write(f"{round(die[0], 3)},{round(die[1], 3)},{die[2]}\n")

    return total_dies, die_data, reticle_yield_counts, reticle_grid


class WsHelper(Parameter):
    serdes = 'u'  # utf8 string (CSV output) - was 'H', a bug on a string state

    def __init__(self, *, config=None, **k):
        super().__init__(**k)
        # Wired JSON config Variable (pull model). An FP here is resolved to the
        # live Variable in Parameter.update(); read on demand via `.state`.
        # An un-wired port arrives as None and falls back to the module DEFAULTS.
        self.config = config  # geometry / edges / ignore_reticles

    def receive_from_gui(self, state):
        self._handle(state)

    def __call__(self, state, **k):
        # wired/programmatic input shares the GUI command path
        self._handle(state)

    def _handle(self, state):
        print(state)
        if isinstance(state, str):
            state = json.loads(state)
        if state['cmd'] == "plot_wafer":
            self.compute_die_yield(
                plot=True,
                heatmap=True,
            )

    def _resolve_config(self) -> dict:
        """Merge a wired `config` Variable over the module DEFAULTS, then resolve
        the `ignore_reticles` key into a set of (ix, iy) tuples. Read live, per
        call, so an edited Variable takes effect on the next plot. Un-wired ->
        pure defaults."""
        cfg = dict(DEFAULTS)
        wired = getattr(self.config, "state", None) if self.config is not None else None
        if wired:
            cfg.update(wired)
        cfg["ignore_reticles"] = {tuple(p) for p in cfg.get("ignore_reticles", [])}
        return cfg

    def compute_die_yield(self, plot: bool = True, heatmap: bool = True, filename: str = "waferspace_run1"):
        cfg = self._resolve_config()
        total_dies, die_data, reticle_yield_counts, reticle_grid = die_yield_advanced(
            die_widths=cfg["die_widths"],
            die_heights=cfg["die_heights"],
            kerf=cfg["kerf"],
            wafer_size=cfg["wafer_size"],
            exclusion_zone=cfg["exclusion_zone"],
            plot=plot,
            heatmap=heatmap,
            filename=filename,
            center_offset_x=cfg["center_offset_x"],
            center_offset_y=cfg["center_offset_y"],
            rotation=cfg["rotation"],
            ignore_reticles=cfg["ignore_reticles"],
            ignore_first_die_col=cfg["ignore_first_die_col"],
            ignore_last_die_col=cfg["ignore_last_die_col"],
            ignore_first_die_row=cfg["ignore_first_die_row"],
            ignore_last_die_row=cfg["ignore_last_die_row"],
        )
        with open(f"{filename}.svg", 'r') as f:
            svg_die = f.read()
        cmd = {"cmd": "die", "data": svg_die}
        self.iris.bifrost.send(self.pid, cmd)

        with open(f"{filename}_heatmap.svg", 'r') as f:
            svg_heatmap = f.read()
        cmd = {"cmd": "heatmap", "data": svg_heatmap}
        self.iris.bifrost.send(self.pid, cmd)

        # Send reticle grid to GUI for the checkbox editor (C5)
        grid_data = [{"ix": k[0], "iy": k[1], "enabled": v} for k, v in reticle_grid.items()]
        cmd = {"cmd": "reticle_grid", "data": grid_data}
        self.iris.bifrost.send(self.pid, cmd)

        with open(f"{filename}.csv", 'r') as f:
            csv_data = f.read()
        self.state = csv_data
        self.emit()

        return total_dies, die_data, reticle_yield_counts, reticle_grid

    def _get_gui(self):
        return {"name": self.name, "pid": self.pid, "state": self.state, "type": "WsHelper"}
