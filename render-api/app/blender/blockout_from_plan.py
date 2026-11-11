"""Trusted Blender blockout. Reads a reviewed motion plan. It does not execute model-written Python."""
import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def argument(name: str) -> str:
    values = sys.argv[sys.argv.index("--") + 1:]
    return values[values.index(name) + 1]


plan = json.loads(Path(argument("--plan")).read_text(encoding="utf-8"))
output = Path(argument("--output"))
duration = max(4, int(plan.get("duration_seconds") or 4))
fps = 12
frame_end = duration * fps

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "BLENDER_EEVEE_NEXT"
scene.render.resolution_x = 640
scene.render.resolution_y = 360
scene.render.fps = fps
scene.frame_start = 1
scene.frame_end = frame_end
scene.render.image_settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.filepath = str(output)
world = bpy.data.worlds.new("blockout-world")
scene.world = world
world.color = (0.05, 0.05, 0.05)

floor_material = bpy.data.materials.new("floor")
floor_material.diffuse_color = (0.18, 0.16, 0.14, 1)
actor_material = bpy.data.materials.new("actor")
actor_material.diffuse_color = (0.2, 0.35, 0.55, 1)

bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 0, 0))
floor = bpy.context.object
floor.data.materials.append(floor_material)

actors = plan.get("actors") or [{"id": "guest"}]
beats = plan.get("action_beats") or [{"seconds": f"0.0-{duration}", "action": "hold"}]
for index, actor in enumerate(actors[:3]):
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.18, depth=1.6, location=(-1.2 + index * 1.2, 0, 0.8))
    body = bpy.context.object
    body.name = str(actor.get("id") or f"actor-{index + 1}")
    body.data.materials.append(actor_material)
    for beat_index, beat in enumerate(beats):
        start_text, end_text = str(beat.get("seconds") or "0-1").split("-", 1)
        frame = max(1, int(round(float(end_text) * fps)))
        side = -1 if beat_index % 2 == 0 else 1
        body.location = Vector((-1.2 + index * 1.2 + side * 0.35, beat_index * 0.25, 0.8))
        body.keyframe_insert(data_path="location", frame=min(frame_end, frame))

camera_data = bpy.data.cameras.new("camera")
camera = bpy.data.objects.new("camera", camera_data)
scene.collection.objects.link(camera)
camera.location = (0, -4.2, 1.7)
direction = Vector((0, 0, 0.8)) - camera.location
camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
scene.camera = camera

scene.render.filepath = str(output)
bpy.ops.render.render(animation=True)
