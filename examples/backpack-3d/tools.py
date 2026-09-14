"""Run complete modeling programs and preserve visually reviewed candidates."""

import base64
import io
import json
import re
import runpy
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, cast

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageOps

import recurse

_AREA_EPSILON = 1e-9
_EDGE_EPSILON = -1e-8
_LINEAR_SRGB_THRESHOLD = 0.0031308
_SRGB_LINEAR_THRESHOLD = 0.04045


def _folder(candidate_id: str) -> Path:
    """Resolve an existing candidate directory without accepting path traversal."""
    path = recurse.context().workspace / candidate_id
    if not re.fullmatch(r"c-\d{3}", candidate_id) or not (path / "result.json").is_file():
        raise ValueError("Unknown candidate ID.")
    return path


def _execute(source: str, destination: str) -> None:
    """Execute the model program, check its geometry, and export a standard Y-up GLB."""
    namespace = runpy.run_path(source, run_name="__model__")
    scene = namespace.get("scene")
    if not isinstance(scene, trimesh.Scene):
        raise TypeError("The program must define scene as a trimesh.Scene.")
    if not scene.geometry:
        raise ValueError("The scene is empty.")
    for mesh in scene.geometry.values():
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            raise ValueError("Every scene geometry must be a nonempty triangle mesh.")
        if not np.isfinite(mesh.vertices).all():
            raise ValueError("Mesh coordinates must be finite.")
    camera = namespace.get("camera", {"azimuth": -115, "elevation": 12})
    if not np.isfinite([camera["azimuth"], camera["elevation"]]).all():
        raise ValueError("Camera angles must be finite.")
    scene.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    scene.export(Path(destination) / "model.glb")
    (Path(destination) / "camera.json").write_text(json.dumps(camera))


def _raster(
    triangles: np.ndarray[Any, Any], normals: np.ndarray[Any, Any], colors: np.ndarray[Any, Any]
) -> np.ndarray[Any, Any]:
    """Rasterize projected triangles with a depth buffer, smooth normals, and sRGB output."""
    pixels = np.full((512, 512, 3), 255, dtype=np.uint8)
    depth = np.full((512, 512), -np.inf)
    light = np.array([-0.4, -0.6, 0.7])
    light /= np.linalg.norm(light)
    for triangle, normal, color in zip(triangles, normals, colors, strict=True):
        a, b, c = triangle
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < _AREA_EPSILON:
            continue
        low = np.maximum(np.floor(triangle[:, :2].min(axis=0)).astype(int), 0)
        high = np.minimum(np.ceil(triangle[:, :2].max(axis=0)).astype(int), 512)
        if (low >= high).any():
            continue
        x, y = np.meshgrid(np.arange(low[0], high[0]) + 0.5, np.arange(low[1], high[1]) + 0.5)
        wa = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
        wb = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
        wc = 1 - wa - wb
        z = wa * a[2] + wb * b[2] + wc * c[2]
        region = np.s_[low[1] : high[1], low[0] : high[0]]
        mask = (
            (wa >= _EDGE_EPSILON)
            & (wb >= _EDGE_EPSILON)
            & (wc >= _EDGE_EPSILON)
            & (z > depth[region])
        )
        smooth = wa[..., None] * normal[0] + wb[..., None] * normal[1] + wc[..., None] * normal[2]
        smooth /= np.maximum(np.linalg.norm(smooth, axis=2, keepdims=True), 1e-9)
        shade = 0.68 + 0.32 * np.maximum(smooth @ light, 0)
        rgb = wa[..., None] * color[0] + wb[..., None] * color[1] + wc[..., None] * color[2]
        linear = np.clip(rgb * shade[..., None] / 255, 0, 1)
        srgb = np.where(
            linear <= _LINEAR_SRGB_THRESHOLD, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055
        )
        pixels[region][mask] = np.round(srgb * 255).astype(np.uint8)[mask]
        depth[region][mask] = z[mask]
    return cast(np.ndarray[Any, Any], pixels[::-1])


def _render(scene: trimesh.Scene, path: Path, azimuth: float, elevation: float) -> None:
    """Project an exported scene and write a diffuse preview from the requested direction."""
    meshes = cast(list[trimesh.Trimesh], scene.dump())
    all_vertices = np.concatenate([mesh.vertices for mesh in meshes])
    center = (all_vertices.min(axis=0) + all_vertices.max(axis=0)) / 2
    az, el = np.radians([azimuth, elevation])
    direction = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    right = np.array([-np.sin(az), np.cos(az), 0])
    up = np.cross(direction, right)
    basis = np.column_stack([right, up, direction])
    positions = (all_vertices - center) @ basis
    scale = 448 / max(float(np.ptp(positions[:, :2], axis=0).max()), 1e-6)
    triangles, normals, colors = [], [], []
    for mesh in meshes:
        screen = (mesh.vertices - center) @ basis
        screen[:, :2] = screen[:, :2] * scale + 256
        visual = mesh.visual
        if isinstance(visual, trimesh.visual.TextureVisuals):
            vertex_colors = np.asarray(visual.to_color().vertex_colors)
            material = cast(trimesh.visual.material.PBRMaterial, visual.material)
            if material.baseColorTexture is not None:
                vertex_colors = vertex_colors.astype(float)
                rgb = vertex_colors[:, :3] / 255
                vertex_colors[:, :3] = (
                    np.where(
                        rgb <= _SRGB_LINEAR_THRESHOLD, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4
                    )
                    * 255
                )
                if material.baseColorFactor is not None:
                    vertex_colors *= np.asarray(material.baseColorFactor) / 255
        else:
            vertex_colors = np.asarray(cast(trimesh.visual.ColorVisuals, visual).vertex_colors)
        vertex_colors = np.broadcast_to(vertex_colors, (len(mesh.vertices), 4))
        triangles.append(screen[mesh.faces])
        normals.append(mesh.vertex_normals[mesh.faces])
        colors.append(vertex_colors[mesh.faces][:, :, :3])
    Image.fromarray(
        _raster(np.concatenate(triangles), np.concatenate(normals), np.concatenate(colors))
    ).save(path)


def reference_image() -> str:
    """Show the actual reference photograph.

    Returns:
        Image data URI with the original PNG or JPEG MIME type, displayed natively by Agentia.
    """
    context = recurse.context()
    encoded = str(context.inputs["reference_image_base64"])
    source = Image.open(io.BytesIO(base64.b64decode(encoded, validate=True)))
    mime = Image.MIME[cast(str, source.format)]
    source.save(context.workspace / "reference.png", format="PNG")
    return f"data:{mime};base64," + encoded


def _build_candidate(code: str, parent_id: str, hypothesis: str) -> str:
    """Execute a complete Python modeling program and independently render its exported mesh.

    Args:
        code: Full Python source defining scene as a trimesh.Scene and optional camera angles.
        parent_id: Previous candidate being revised, or empty for an independent construction.
        hypothesis: Concrete modeling approach or visual defect this attempt addresses.

    Returns:
        JSON candidate ID, validity, mesh statistics, or execution errors. Use view_candidate next.
    """
    context = recurse.context()
    if parent_id:
        _folder(parent_id)
    count = len(list(context.workspace.glob("c-*/result.json")))
    if count >= int(str(context.inputs["max_trials"])):
        raise ValueError("Candidate budget exhausted; review and finish.")
    candidate_id = f"c-{count + 1:03d}"
    folder = context.workspace / candidate_id
    folder.mkdir()
    source = folder / "source.py"
    source.write_text(code)
    result = {
        "candidate_id": candidate_id,
        "parent_id": parent_id,
        "hypothesis": hypothesis,
        "valid": False,
    }
    (folder / "result.json").write_text(json.dumps(result))
    started = time.monotonic()
    runner = (
        "import site, sys, types; site.addsitedir(sys.argv[4]); "
        "sys.path.insert(0, sys.argv[1]); "
        "sys.modules['recurse'] = types.ModuleType('recurse'); import tools; "
        "tools._execute(sys.argv[2], sys.argv[3])"
    )
    try:
        process = subprocess.run(  # noqa: S603 - modeling code executes in the Recurse sandbox
            [
                sys.executable,
                "-c",
                runner,
                str(Path(__file__).parent),
                str(source),
                str(folder),
                str(Path(trimesh.__file__).parent.parent),
            ],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        (folder / "execution.log").write_text(process.stdout + process.stderr)
        if process.returncode:
            raise ValueError((process.stdout + process.stderr)[-3000:])
        scene = trimesh.load_scene(folder / "model.glb")
        scene.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
        camera = json.loads((folder / "camera.json").read_text())
        for name, angle in [("front", 0), ("side", 65), ("back", 180)]:
            _render(scene, folder / f"{name}.png", camera["azimuth"] + angle, camera["elevation"])
        result.update(
            valid=True,
            triangles=sum(len(m.faces) for m in scene.geometry.values()),
            meshes=len(scene.geometry),
            bounds=scene.bounds.tolist(),
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        result["error"] = str(error)[-3000:]
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (folder / "result.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "valid": result["valid"],
                "elapsed_seconds": result["elapsed_seconds"],
            }
        ),
        flush=True,
    )
    return json.dumps(result)


def build_candidate(code: str, parent_id: str, hypothesis: str) -> str:
    """Execute a complete modeling program and immediately show its actual mesh renders.

    Args:
        code: Complete Python program defining a trimesh.Scene and optional camera.
        parent_id: Earlier candidate being revised, or empty for an independent construction.
        hypothesis: Specific visual improvement or construction approach to test.

    Returns:
        Native image showing the candidate ID, reference, front, side and back; JSON on failure.
    """
    result = json.loads(_build_candidate(code, parent_id, hypothesis))
    if result["valid"]:
        return view_candidate(result["candidate_id"])
    return json.dumps(result)


def read_candidate(candidate_id: str) -> str:
    """Read the complete editable program of a saved candidate.

    Args:
        candidate_id: Candidate ID returned by build_candidate.

    Returns:
        JSON source code and build result.
    """
    folder = _folder(candidate_id)
    return json.dumps(
        {
            "code": (folder / "source.py").read_text(),
            "result": json.loads((folder / "result.json").read_text()),
        }
    )


def revise_candidate(parent_id: str, edits_json: str, hypothesis: str) -> str:
    """Revise saved Python source using exact text replacements, then execute and render it.

    Args:
        parent_id: Existing candidate whose program should be revised.
        edits_json: JSON array of old/new strings. Each old string must occur exactly once.
        hypothesis: Concrete visual defect or modeling change being tested.

    Returns:
        Native candidate image on success, or JSON execution error. The parent is preserved.
    """
    code = (_folder(parent_id) / "source.py").read_text()
    for edit in json.loads(edits_json):
        if code.count(edit["old"]) != 1:
            raise ValueError("Each old string must occur exactly once in the current source.")
        code = code.replace(edit["old"], edit["new"], 1)
    return build_candidate(code, parent_id, hypothesis)


def view_candidate(candidate_id: str) -> str:
    """Show the reference, matched camera, side, and back together for visual critique.

    Args:
        candidate_id: Valid candidate to inspect.

    Returns:
        Native JPEG image attachment with labeled reference and three actual mesh renders.
    """
    folder = _folder(candidate_id)
    if not json.loads((folder / "result.json").read_text())["valid"]:
        raise ValueError("Only a valid candidate has rendered views.")
    reference_image()
    sheet = Image.new("RGB", (1024, 1024), "white")
    panels = [
        ("REFERENCE", folder.parent / "reference.png"),
        (candidate_id + " FRONT", folder / "front.png"),
        (candidate_id + " SIDE", folder / "side.png"),
        (candidate_id + " BACK", folder / "back.png"),
    ]
    for index, (label, path) in enumerate(panels):
        x, y = (index % 2) * 512, (index // 2) * 512
        panel = ImageOps.contain(Image.open(path).convert("RGB"), (492, 480))
        sheet.paste(panel, (x + (512 - panel.width) // 2, y + 28 + (480 - panel.height) // 2))
        ImageDraw.Draw(sheet).text((x + 10, y + 8), label, fill="black")
    output = io.BytesIO()
    sheet.save(output, format="JPEG", quality=82)
    (folder / "comparison.jpg").write_bytes(output.getvalue())
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()


def record_review(candidate_id: str, critique: str, keep: bool) -> str:
    """Record a visual comparison and optionally preserve this candidate as the incumbent.

    Args:
        candidate_id: Candidate just inspected with view_candidate.
        critique: Specific visible strengths, defects, and comparison with the incumbent.
        keep: Whether the inspected candidate should replace the current incumbent.

    Returns:
        JSON review and selected candidate ID, if any. No pixel score determines selection.
    """
    folder = _folder(candidate_id)
    if not (folder / "comparison.jpg").is_file():
        raise ValueError("Call view_candidate before recording a visual review.")
    review = {"candidate_id": candidate_id, "critique": critique, "keep": keep}
    (folder / "review.json").write_text(json.dumps(review, indent=2))
    if keep:
        for original, saved in [
            ("model.glb", "best.glb"),
            ("source.py", "best.py"),
            ("front.png", "best.png"),
            ("comparison.jpg", "best-comparison.jpg"),
        ]:
            shutil.copyfile(folder / original, folder.parent / saved)
        (folder.parent / "selection.json").write_text(json.dumps(review, indent=2))
    count = len(list(folder.parent.glob("c-*/result.json")))
    if count in (1, 4, 16, 32) and (folder.parent / "selection.json").is_file():
        checkpoint = folder.parent / f"checkpoint-{count:03d}"
        checkpoint.mkdir(exist_ok=True)
        for original, saved in [
            ("best.glb", "model.glb"),
            ("best.py", "source.py"),
            ("best.png", "front.png"),
            ("selection.json", "selection.json"),
        ]:
            shutil.copyfile(folder.parent / original, checkpoint / saved)
    return json.dumps(review)


def finish_run() -> str:
    """Archive every attempt and return the selected model receipt.

    Returns:
        JSON candidate ID, total attempts including failures, and number of valid candidates.
    """
    root = recurse.context().workspace
    if not (root / "selection.json").is_file():
        raise ValueError("No visually reviewed candidate has been selected.")
    selection = json.loads((root / "selection.json").read_text())
    results = [json.loads(p.read_text()) for p in sorted(root.glob("c-*/result.json"))]
    receipt = {
        "candidate_id": selection["candidate_id"],
        "attempts": len(results),
        "valid_candidates": sum(r["valid"] for r in results),
    }
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2))
    with zipfile.ZipFile(root / "history.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in sorted(root.glob("c-*/")):
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root))
    for folder in root.glob("c-*/"):
        shutil.rmtree(folder)
    return json.dumps(receipt)
