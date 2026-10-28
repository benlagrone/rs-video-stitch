"""Blender-side renderer for a constrained MediaStudio 3D shot brief."""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--poster", required=True)
    parser.add_argument("--blend", required=True)
    values = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return parser.parse_args(values)


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def color_for(name):
    colors = {
        "rocky-planet": (0.22, 0.07, 0.025, 1.0),
        "dark-rock": (0.025, 0.035, 0.055, 1.0),
        "stone": (0.19, 0.23, 0.28, 1.0),
        "water": (0.02, 0.12, 0.25, 1.0),
        "gold": (0.65, 0.27, 0.035, 1.0),
    }
    return colors.get(name, (0.18, 0.24, 0.30, 1.0))


def material(name):
    value = bpy.data.materials.new(name)
    value.use_nodes = True
    principled = value.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = color_for(name)
    principled.inputs["Roughness"].default_value = 0.66
    principled.inputs["Metallic"].default_value = 0.08
    if name == "rocky-planet":
        noise = value.node_tree.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 3.8
        noise.inputs["Detail"].default_value = 7.0
        noise.inputs["Roughness"].default_value = 0.72
        ramp = value.node_tree.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = (0.008, 0.012, 0.025, 1.0)
        ramp.color_ramp.elements[1].color = (0.38, 0.08, 0.015, 1.0)
        bump = value.node_tree.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.35
        bump.inputs["Distance"].default_value = 0.18
        value.node_tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        value.node_tree.links.new(ramp.outputs["Color"], principled.inputs["Base Color"])
        value.node_tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
        value.node_tree.links.new(bump.outputs["Normal"], principled.inputs["Normal"])
    return value


def add_object(spec, mats):
    primitive = spec.get("primitive")
    if primitive == "sphere":
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=5, radius=1.0)
    elif primitive == "plane":
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=72, y_subdivisions=72, size=2.0)
        obj = bpy.context.object
        for vertex in obj.data.vertices:
            vertex.co.z = 0.08 * math.sin(vertex.co.x * 3.2) * math.cos(vertex.co.y * 2.7)
    elif primitive == "cube":
        bpy.ops.mesh.primitive_cube_add(size=2.0)
    elif primitive == "cone":
        bpy.ops.mesh.primitive_cone_add(vertices=48, radius1=1.0, radius2=0.08, depth=2.0)
    elif primitive == "torus":
        bpy.ops.mesh.primitive_torus_add(major_radius=1.0, minor_radius=0.12, major_segments=64, minor_segments=16)
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1.0)
    obj = bpy.context.object
    obj.name = spec.get("id", "object")
    obj.scale = spec.get("scale", [1, 1, 1])
    obj.location = spec.get("startPosition", [0, 0, 1])
    obj.rotation_euler = [math.radians(value) for value in spec.get("rotationDegrees", [0, 0, 0])]
    mat_name = spec.get("material", "stone")
    if mat_name not in mats:
        mats[mat_name] = material(mat_name)
    obj.data.materials.append(mats[mat_name])
    obj.keyframe_insert(data_path="location", frame=1)
    obj.keyframe_insert(data_path="rotation_euler", frame=1)
    obj.location = spec.get("endPosition", obj.location)
    obj.rotation_euler.z += math.radians(28 if primitive == "sphere" else 3)
    obj.keyframe_insert(data_path="location", frame=81)
    obj.keyframe_insert(data_path="rotation_euler", frame=81)
    if obj.animation_data and obj.animation_data.action:
        for curve in obj.animation_data.action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "BEZIER"
    return obj


def main():
    options = args()
    brief = json.loads(Path(options.brief).read_text(encoding="utf-8"))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = 48
    scene.render.resolution_x = 576
    scene.render.resolution_y = 320
    scene.render.resolution_percentage = 100
    scene.render.fps = 16
    scene.frame_start = 1
    scene.frame_end = 81
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
    video_target = Path(options.video)
    animation_prefix = video_target.with_suffix("")
    scene.render.filepath = f"{animation_prefix}-"
    scene.world = bpy.data.worlds.new("primordial-world")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.002, 0.004, 0.012, 1.0)
    scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.11

    random.seed(1101)
    star_mat = bpy.data.materials.new("stars")
    star_mat.use_nodes = True
    star_bsdf = star_mat.node_tree.nodes.get("Principled BSDF")
    star_bsdf.inputs["Base Color"].default_value = (1.0, 0.84, 0.55, 1.0)
    emission_input = "Emission Color" if "Emission Color" in star_bsdf.inputs else "Emission"
    star_bsdf.inputs[emission_input].default_value = (1.0, 0.55, 0.12, 1.0)
    if "Emission Strength" in star_bsdf.inputs:
        star_bsdf.inputs["Emission Strength"].default_value = 5.0
    for index in range(70):
        bpy.ops.mesh.primitive_ico_sphere_add(
            subdivisions=1,
            radius=random.uniform(0.006, 0.022),
            location=(random.uniform(-5.5, 5.5), random.uniform(1.8, 4.5), random.uniform(1.0, 5.5)),
        )
        star = bpy.context.object
        star.name = f"star-{index:03d}"
        star.data.materials.append(star_mat)

    mats = {}
    for spec in brief.get("objects", []):
        add_object(spec, mats)
    bpy.ops.object.light_add(type="AREA", location=(-4.0, -3.0, 6.5))
    key = bpy.context.object
    key.data.energy = 1050
    key.data.color = (0.55, 0.68, 1.0)
    key.data.size = 5.5
    look_at(key, (0.0, 0.0, 1.5))
    bpy.ops.object.light_add(type="POINT", location=(3.8, -0.5, 2.2))
    rim = bpy.context.object
    rim.data.energy = 1450
    rim.data.color = (1.0, 0.15, 0.02)
    rim.data.shadow_soft_size = 2.0

    camera_spec = brief.get("camera") or {}
    bpy.ops.object.camera_add(location=camera_spec.get("position", [0.0, -9.0, 3.2]))
    camera = bpy.context.object
    camera.data.lens = float(camera_spec.get("lensMm", 48))
    look_at(camera, camera_spec.get("target", [0.0, 0.0, 1.7]))
    scene.camera = camera
    bpy.ops.wm.save_as_mainfile(filepath=options.blend)

    video_target.parent.mkdir(parents=True, exist_ok=True)
    Path(options.poster).parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = f"{animation_prefix}-"
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format = "MPEG4"
    scene.render.ffmpeg.codec = "H264"
    bpy.ops.render.render(animation=True)
    candidates = sorted(
        path for path in animation_prefix.parent.glob(f"{animation_prefix.name}-*")
        if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi"}
    )
    if not candidates:
        raise RuntimeError("Blender rendered frames but did not create an animation container")
    rendered_video = candidates[-1]
    if rendered_video.suffix.lower() == ".mp4":
        rendered_video.replace(video_target)
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(rendered_video), "-c:v", "copy", "-an", str(video_target)],
            check=True,
            capture_output=True,
        )
    scene.frame_set(1)
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = options.poster
    bpy.ops.render.render(write_still=True)


if __name__ == "__main__":
    main()
