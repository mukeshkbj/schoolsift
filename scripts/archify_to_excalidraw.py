"""Convert an Archify architecture spec into an editable Excalidraw file.

Usage: python3 scripts/archify_to_excalidraw.py <spec.json> <out.excalidraw>

Only the `architecture` diagram type is supported: components become
labelled rectangles, boundaries become dashed frames, and connections become
arrows bound to both endpoints so they follow the shapes when edited.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

FILL = {
    "frontend": ("#0891b2", "#cffafe"),
    "backend": ("#059669", "#d1fae5"),
    "database": ("#7c3aed", "#ede9fe"),
    "cloud": ("#d97706", "#fef3c7"),
    "security": ("#e11d48", "#ffe4e6"),
    "messagebus": ("#ea580c", "#ffedd5"),
    "external": ("#475569", "#e2e8f0"),
}
ARROW = {
    "default": "#64748b",
    "emphasis": "#059669",
    "security": "#e11d48",
    "dashed": "#7c3aed",
}
NOW = int(time.time() * 1000)


def base(
    kind: str, x: float, y: float, w: float, h: float, **extra: Any
) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": kind,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": "#1e293b",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": abs(hash((kind, x, y))) % 2_000_000_000,
        "version": 1,
        "versionNonce": abs(hash((x, y, kind, w))) % 2_000_000_000,
        "isDeleted": False,
        "boundElements": [],
        "updated": NOW,
        "link": None,
        "locked": False,
    }
    element.update(extra)
    return element


def text(
    element_id: str,
    x: float,
    y: float,
    w: float,
    h: float,
    value: str,
    size: int,
    container: str | None,
) -> dict[str, Any]:
    return base(
        "text",
        x,
        y,
        w,
        h,
        id=element_id,
        text=value,
        originalText=value,
        fontSize=size,
        fontFamily=1,
        textAlign="center" if container else "left",
        verticalAlign="middle" if container else "top",
        containerId=container,
        lineHeight=1.25,
        autoResize=True,
    )


def anchor(rect: dict[str, Any], other: dict[str, Any]) -> tuple[float, float]:
    """Midpoint of the rectangle side that faces the other rectangle."""
    x, y, w, h = (float(rect[k]) for k in ("x", "y", "width", "height"))
    ox, oy = (
        float(other["x"]) + float(other["width"]) / 2,
        float(other["y"]) + float(other["height"]) / 2,
    )
    cx, cy = x + w / 2, y + h / 2
    if abs(ox - cx) * h > abs(oy - cy) * w:
        return (x + w if ox > cx else x, cy)
    return (cx, y + h if oy > cy else y)


def convert(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("diagram_type") != "architecture":
        raise SystemExit("only architecture specs are supported")
    elements: list[dict[str, Any]] = []
    rects: dict[str, dict[str, Any]] = {}

    for boundary in spec.get("boundaries", []):
        wrapped = [c for c in spec["components"] if c["id"] in boundary["wraps"]]
        pad = float(boundary.get("pad", 20)) + 12
        x0 = min(float(c["pos"][0]) for c in wrapped) - pad
        y0 = min(float(c["pos"][1]) for c in wrapped) - pad
        x1 = max(float(c["pos"][0]) + float(c["size"][0]) for c in wrapped) + pad
        y1 = max(float(c["pos"][1]) + float(c["size"][1]) for c in wrapped) + pad
        color = "#e11d48" if boundary["kind"] == "security-group" else "#d97706"
        frame_id = f"boundary-{len(elements)}"
        elements.append(
            base(
                "rectangle",
                x0,
                y0,
                x1 - x0,
                y1 - y0,
                id=frame_id,
                strokeColor=color,
                strokeStyle="dashed",
                roundness={"type": 3},
            )
        )
        elements.append(
            text(
                f"{frame_id}-label",
                x0 + 8,
                y0 - 22,
                8 * len(boundary["label"]),
                20,
                boundary["label"],
                14,
                None,
            )
        )
        elements[-1]["strokeColor"] = color

    for component in spec["components"]:
        stroke, fill = FILL[component["type"]]
        x, y = (float(v) for v in component["pos"])
        w, h = (float(v) for v in component["size"])
        rect = base(
            "rectangle",
            x,
            y,
            w,
            h,
            id=component["id"],
            strokeColor=stroke,
            backgroundColor=fill,
            roundness={"type": 3},
        )
        label = component["label"] + (
            f"\n{component['sublabel']}" if component.get("sublabel") else ""
        )
        rect["boundElements"] = [{"id": f"{component['id']}-text", "type": "text"}]
        rects[component["id"]] = rect
        elements.append(rect)
        elements.append(
            text(
                f"{component['id']}-text",
                x + 8,
                y + 8,
                w - 16,
                h - 16,
                label,
                14,
                component["id"],
            )
        )

    for index, connection in enumerate(spec.get("connections", [])):
        source, target = rects[connection["from"]], rects[connection["to"]]
        sx, sy = anchor(source, target)
        tx, ty = anchor(target, source)
        arrow_id = f"arrow-{index}"
        variant = connection.get("variant", "default")
        arrow = base(
            "arrow",
            sx,
            sy,
            tx - sx,
            ty - sy,
            id=arrow_id,
            points=[[0, 0], [tx - sx, ty - sy]],
            strokeColor=ARROW[variant],
            strokeWidth=2 if variant == "emphasis" else 1,
            strokeStyle="dashed" if variant == "dashed" else "solid",
            startBinding={"elementId": source["id"], "focus": 0, "gap": 4},
            endBinding={"elementId": target["id"], "focus": 0, "gap": 4},
            startArrowhead=None,
            endArrowhead="arrow",
            roundness={"type": 2},
        )
        source["boundElements"].append({"id": arrow_id, "type": "arrow"})
        target["boundElements"].append({"id": arrow_id, "type": "arrow"})
        elements.append(arrow)
        if connection.get("label"):
            arrow["boundElements"] = [{"id": f"{arrow_id}-text", "type": "text"}]
            elements.append(
                text(
                    f"{arrow_id}-text",
                    sx + (tx - sx) / 2 - 40,
                    sy + (ty - sy) / 2 - 10,
                    80,
                    20,
                    connection["label"],
                    12,
                    arrow_id,
                )
            )

    return {
        "type": "excalidraw",
        "version": 2,
        "source": "schoolsift/scripts/archify_to_excalidraw.py",
        "elements": elements,
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20},
        "files": {},
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    spec = json.loads(Path(sys.argv[1]).read_text())
    Path(sys.argv[2]).write_text(json.dumps(convert(spec), indent=2) + "\n")


if __name__ == "__main__":
    main()
