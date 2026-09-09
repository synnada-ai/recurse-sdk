"""Exercise image input, program execution, rendering, revisions, and saved artifacts."""

import base64
import io
import json
import shlex
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import numpy as np
import pytest
import trimesh
from PIL import Image

import recurse
import tools

PROGRAM = """import trimesh
from trimesh.visual.material import PBRMaterial
from trimesh.visual import TextureVisuals
scene = trimesh.Scene()
mesh = trimesh.creation.box(extents=[0.3, 0.12, 0.4])
mesh.apply_translation([0, 0, 0.2])
mesh.visual = TextureVisuals(material=PBRMaterial(baseColorFactor=[95, 40, 75, 255]))
scene.add_geometry(mesh, node_name="body")
camera = {"azimuth": -115, "elevation": 12}
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """Provide an active run with a reference photograph and a three-attempt budget."""
    stream = io.BytesIO()
    Image.new("RGB", (100, 120), "white").save(stream, format="PNG")
    recurse._activate(
        {"reference_image_base64": base64.b64encode(stream.getvalue()).decode(), "max_trials": 3},
        tmp_path,
    )
    yield tmp_path
    recurse._deactivate()


def test_native_reference_contains_original_pixels(workspace: Path) -> None:
    """Native reference contains original pixels."""
    uri = tools.reference_image()
    assert uri.startswith("data:image/png;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(uri.split(",")[1])))
    assert image.size == (100, 120)
    assert (workspace / "reference.png").is_file()


def test_build_tool_returns_rendered_image_without_an_extra_model_call(workspace: Path) -> None:
    """Build tool returns rendered image without an extra model call."""
    reply = tools.build_candidate(PROGRAM, "", "first complete construction")
    assert reply.startswith("data:image/jpeg;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(reply.split(",", 1)[1])))
    assert image.size == (1024, 1024)
    assert (workspace / "c-001/comparison.jpg").is_file()
    broken = json.loads(tools.build_candidate("raise ValueError('broken')", "", "failure"))
    assert not broken["valid"]


def test_full_program_exports_a_real_scene_and_independent_views(workspace: Path) -> None:
    """Full program exports a real scene and independent views."""
    result = json.loads(tools._build_candidate(PROGRAM, "", "test complete program"))
    assert result["valid"] is True
    assert result["candidate_id"] == "c-001"
    folder = workspace / "c-001"
    mesh = trimesh.load(folder / "model.glb", force="mesh")
    # The exported glTF is Y-up; source Z height becomes its Y dimension.
    assert np.allclose(mesh.extents, [0.3, 0.4, 0.12])
    front = np.array(Image.open(folder / "front.png"))
    side = np.array(Image.open(folder / "side.png"))
    assert front.shape == (512, 512, 3)
    assert (front < 230).any()
    assert not np.array_equal(front, side)
    assert json.loads(tools.read_candidate("c-001"))["code"] == PROGRAM


def test_program_subprocess_loads_application_dependencies_without_parent_site_setup(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Program subprocess loads application dependencies without parent site setup."""
    executable = workspace / "bare-python"
    executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -S "$@"\n')
    executable.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(executable))
    result = json.loads(tools._build_candidate(PROGRAM, "", "isolated runtime"))
    assert result["valid"], result.get("error")


def test_jpeg_reference_keeps_pixels_and_is_sent_with_correct_mime(workspace: Path) -> None:
    """Jpeg reference keeps pixels and is sent with correct mime."""
    stream = io.BytesIO()
    Image.new("RGB", (100, 120), (50, 80, 100)).save(stream, format="JPEG")
    expected = np.array(Image.open(io.BytesIO(stream.getvalue())))
    recurse._deactivate()
    recurse._activate(
        {"reference_image_base64": base64.b64encode(stream.getvalue()).decode()}, workspace
    )
    uri = tools.reference_image()
    assert uri.startswith("data:image/jpeg;base64,")
    assert np.array_equal(np.array(Image.open(workspace / "reference.png")), expected)
    assert Image.open(workspace / "reference.png").format == "PNG"


def test_custom_mesh_code_is_not_restricted_to_fixed_primitives(workspace: Path) -> None:
    """Custom mesh code is not restricted to fixed primitives."""
    code = """import numpy as np, trimesh
v=np.array([[0,0,0],[1,0,0],[0,1,0],[0,0,1]], dtype=float)
scene=trimesh.Scene(trimesh.Trimesh(v, [[0,2,1],[0,1,3],[0,3,2],[1,2,3]]))
"""
    result = json.loads(tools._build_candidate(code, "", "custom tetrahedron"))
    assert result["valid"]
    assert result["triangles"] == 4


def test_trimesh_normal_repair_is_available_to_modeling_programs(workspace: Path) -> None:
    """Trimesh normal repair is available to modeling programs."""
    code = PROGRAM.replace(
        "scene.add_geometry(mesh",
        "mesh.faces[0]=mesh.faces[0][::-1]\nmesh.fix_normals()\nscene.add_geometry(mesh",
    )
    result = json.loads(tools._build_candidate(code, "", "repair inconsistent normals"))
    assert result["valid"], result.get("error")
    mesh = cast(trimesh.Trimesh, trimesh.load(workspace / "c-001/model.glb", force="mesh"))
    assert mesh.is_winding_consistent
    assert mesh.volume == pytest.approx(0.0144)


def test_failed_program_preserved_and_counts_toward_budget(workspace: Path) -> None:
    """Failed program preserved and counts toward budget."""
    for _ in range(3):
        result = json.loads(tools._build_candidate("raise RuntimeError('deliberate')", "", "bad"))
        assert result["valid"] is False
        assert "deliberate" in result["error"]
    assert len(list(workspace.glob("c-*/source.py"))) == 3
    with pytest.raises(ValueError, match="budget"):
        tools._build_candidate(PROGRAM, "", "over budget")


@pytest.mark.parametrize("candidate", ["../escape", "c-999", "reference.png"])
def test_unknown_or_escaping_candidate_is_rejected(workspace: Path, candidate: str) -> None:
    """Unknown or escaping candidate is rejected."""
    with pytest.raises(ValueError):
        tools.read_candidate(candidate)


def test_revision_uses_saved_source_and_preserves_parent(workspace: Path) -> None:
    """Revision uses saved source and preserves parent."""
    tools._build_candidate(PROGRAM, "", "initial")
    edit = json.dumps([{"old": "0.3, 0.12, 0.4", "new": "0.25, 0.12, 0.4"}])
    reply = tools.revise_candidate("c-001", edit, "narrow body")
    assert reply.startswith("data:image/jpeg;base64,")
    child = json.loads((workspace / "c-002/result.json").read_text())
    assert child["parent_id"] == "c-001"
    assert (workspace / "c-001/source.py").read_text() == PROGRAM
    assert "0.25, 0.12, 0.4" in (workspace / "c-002/source.py").read_text()
    for old in ["not present", "scene"]:
        with pytest.raises(ValueError, match="exactly once"):
            tools.revise_candidate("c-001", json.dumps([{"old": old, "new": "x"}]), "bad patch")
    assert not (workspace / "c-003").exists()


def test_selection_requires_actual_view_and_preserves_best_after_rejection(workspace: Path) -> None:
    """Selection requires actual view and preserves best after rejection."""
    tools.reference_image()
    tools._build_candidate(PROGRAM, "", "first")
    with pytest.raises(ValueError, match="view"):
        tools.record_review("c-001", "good", True)
    uri = tools.view_candidate("c-001")
    sheet = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    assert sheet.size == (1024, 1024)
    tools.record_review("c-001", "not selected yet", False)
    assert not (workspace / "checkpoint-001").exists()
    tools.record_review("c-001", "first baseline", True)
    original = (workspace / "best.glb").read_bytes()
    assert (workspace / "checkpoint-001/model.glb").read_bytes() == original
    tools._build_candidate(PROGRAM.replace("0.3, 0.12, 0.4", "0.05, 0.12, 0.4"), "c-001", "thin")
    tools.view_candidate("c-002")
    tools.record_review("c-002", "too narrow; keep previous", False)
    (workspace / "c-002/details").mkdir()
    (workspace / "c-002/details/note.txt").write_text("retained auxiliary artifact")
    assert (workspace / "best.glb").read_bytes() == original
    receipt = json.loads(tools.finish_run())
    assert receipt == {"candidate_id": "c-001", "attempts": 2, "valid_candidates": 2}
    with zipfile.ZipFile(workspace / "history.zip") as archive:
        assert "c-001/source.py" in archive.namelist()
        assert "c-002/review.json" in archive.namelist()
        assert archive.read("c-002/details/note.txt") == b"retained auxiliary artifact"
        assert json.loads(archive.read("c-002/review.json"))["keep"] is False
    assert not list(workspace.glob("c-*/"))


def test_missing_selection_and_invalid_candidate_cannot_finish_or_be_reviewed(
    workspace: Path,
) -> None:
    """Missing selection and invalid candidate cannot finish or be reviewed."""
    with pytest.raises(ValueError, match="selected"):
        tools.finish_run()
    tools._build_candidate("raise ValueError('broken')", "", "bad")
    with pytest.raises(ValueError, match="valid"):
        tools.view_candidate("c-001")


@pytest.mark.parametrize(
    "code,message",
    [("scene=None", "trimesh.Scene"), ("import trimesh; scene=trimesh.Scene()", "empty")],
)
def test_invalid_model_is_a_recorded_failure(workspace: Path, code: str, message: str) -> None:
    """Invalid model is a recorded failure."""
    result = json.loads(tools._build_candidate(code, "", "bad model"))
    assert not result["valid"]
    assert message in result["error"]


def test_program_execution_preserves_materials_and_exports_y_up(tmp_path: Path) -> None:
    """Program execution preserves materials and exports y up."""
    source = tmp_path / "source.py"
    source.write_text(PROGRAM)
    tools._execute(str(source), str(tmp_path))
    loaded = trimesh.load_scene(tmp_path / "model.glb")
    assert np.allclose(loaded.extents, [0.3, 0.4, 0.12])
    material = next(iter(loaded.geometry.values())).visual.material
    assert list(material.baseColorFactor) == [95, 40, 75, 255]
    assert json.loads((tmp_path / "camera.json").read_text())["azimuth"] == -115
    source.write_text(PROGRAM.split("camera =", maxsplit=1)[0])
    tools._execute(str(source), str(tmp_path))
    assert (tmp_path / "camera.json").is_file()


@pytest.mark.parametrize(
    "code,message",
    [
        ("scene=None", "trimesh.Scene"),
        ("import trimesh; scene=trimesh.Scene()", "empty"),
        (
            "import trimesh; scene=trimesh.Scene(); scene.geometry['bad']=trimesh.Trimesh()",
            "nonempty",
        ),
        (PROGRAM + "\nscene.geometry['bad'] = object()", "nonempty"),
        (PROGRAM + "\nmesh.vertices[0,0] = float('nan')", "finite"),
        (PROGRAM + "\ncamera['azimuth'] = float('nan')", "finite"),
    ],
)
def test_program_validation_rejects_invalid_scene_before_export(
    tmp_path: Path, code: str, message: str
) -> None:
    """Program validation rejects invalid scene before export."""
    source = tmp_path / "source.py"
    source.write_text(code)
    with pytest.raises((ValueError, TypeError), match=message):
        tools._execute(str(source), str(tmp_path))
    assert not (tmp_path / "model.glb").exists()


def test_raster_discards_degenerate_and_offscreen_faces_and_resolves_occlusion() -> None:
    """Raster discards degenerate and offscreen faces and resolves occlusion."""
    triangles = np.array(
        [
            [[10, 10, 0], [10, 10, 0], [10, 10, 0]],
            [[600, 600, 0], [700, 600, 0], [600, 700, 0]],
            [[20, 20, 0], [400, 20, 0], [20, 400, 0]],
            [[20, 20, 1], [400, 20, 1], [20, 400, 1]],
        ],
        dtype=float,
    )
    normals = np.tile([0, 0, 1], (4, 3, 1))
    colors = np.array([[[255, 0, 0]] * 3] * 3 + [[[0, 0, 255]] * 3])
    pixels = tools._raster(triangles, normals, colors)
    assert pixels[511 - 100, 100, 2] > 200
    assert pixels[511 - 100, 100, 0] == 0
    assert list(pixels[0, 0]) == [255, 255, 255]


@pytest.mark.parametrize(
    "factor,texture,expected",
    [
        (55, None, 128),
        (255, 128, 128),
        (128, 128, 93),
        (None, 128, 128),
        (255, 10, 10),
    ],
)
def test_preview_displays_gltf_linear_material_colors_in_srgb(
    tmp_path: Path, factor: int | None, texture: int | None, expected: int
) -> None:
    """Preview displays gltf linear material colors in srgb."""
    light = np.array([-0.4, -0.6, 0.7])
    light /= np.linalg.norm(light)
    right = np.cross(light, [0, 0, 1])
    right /= np.linalg.norm(right)
    up = np.cross(light, right)
    vertices = np.array([-right - up, right - up, up])
    mesh = trimesh.Trimesh(vertices=vertices, faces=[[0, 1, 2]], process=False)
    mesh.vertex_normals = np.tile(light, (3, 1))  # type: ignore[method-assign]  # Trimesh allows this cached-property assignment.
    mesh.visual = trimesh.visual.TextureVisuals(
        uv=np.zeros((3, 2)),
        material=trimesh.visual.material.PBRMaterial(
            baseColorFactor=None if factor is None else [factor, factor, factor, 255],
            baseColorTexture=None if texture is None else Image.new("RGB", (2, 2), (texture,) * 3),
        ),
    )
    azimuth = np.degrees(np.arctan2(light[1], light[0]))
    elevation = np.degrees(np.arcsin(light[2]))
    tools._render(trimesh.Scene(mesh), tmp_path / "render.png", azimuth, elevation)
    # Linear 55/255 is approximately sRGB 128/255 under full diffuse illumination.
    pixel = np.array(Image.open(tmp_path / "render.png"))[256, 256]
    assert np.all(abs(pixel.astype(int) - expected) <= 1), pixel
