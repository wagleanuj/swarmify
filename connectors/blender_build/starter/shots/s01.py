"""Shot s01: a lit cube turning in front of a camera, using only bpy.

The project contract (blender_render's README): Blender runs this script headless with
BLENDER_PROJECT (this project), BLENDER_SHOT_OUT (where frames go), BLENDER_SHOT_QUALITY (a preset
of render.json) and BLENDER_SHOT_FRAMES (the frames to render, empty for all). It writes
frame_NNNN.png per frame at render.json's width and height.
"""

import json
import math
import os
from pathlib import Path

import bpy

LENGTH = 60  # frames: 2 s at render.json's 30 fps

project = Path(os.environ["BLENDER_PROJECT"])
spec = json.loads((project / "render.json").read_text())
preset = spec["quality"][os.environ.get("BLENDER_SHOT_QUALITY", "draft")]
scene = bpy.context.scene

# A clean scene: factory startup has its own cube, light and camera.
for thing in list(bpy.data.objects):
    bpy.data.objects.remove(thing)

bpy.ops.mesh.primitive_cube_add(size=2)
cube = bpy.context.object
material = bpy.data.materials.new("Cube")
material.use_nodes = True
shader = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
shader.inputs["Base Color"].default_value = (0.9, 0.35, 0.1, 1)
cube.data.materials.append(material)
cube.rotation_euler = (0, 0, 0)
cube.keyframe_insert("rotation_euler", frame=1)
cube.rotation_euler = (0, 0, math.pi / 2)
cube.keyframe_insert("rotation_euler", frame=LENGTH)

bpy.ops.object.light_add(type="AREA", location=(3, -4, 6))
bpy.context.object.data.energy = 800
bpy.context.object.data.size = 4

bpy.ops.object.camera_add(location=(0, -10, 3))
camera = bpy.context.object
aim = camera.constraints.new("TRACK_TO")
aim.target = cube
scene.camera = camera

background = next(n for n in scene.world.node_tree.nodes if n.type == "BACKGROUND")
background.inputs["Color"].default_value = (0.05, 0.06, 0.08, 1)

scene.render.resolution_x, scene.render.resolution_y = spec["width"], spec["height"]
scene.render.resolution_percentage = 100
scene.render.fps = spec["fps"]
scene.eevee.taa_render_samples = preset["samples"]
scene.render.image_settings.file_format = "PNG"

out = Path(os.environ["BLENDER_SHOT_OUT"])
listed = os.environ.get("BLENDER_SHOT_FRAMES", "")
for n in [int(f) for f in listed.split(",")] if listed else range(1, LENGTH + 1):
    scene.frame_set(n)
    scene.render.filepath = str(out / f"frame_{n:04d}.png")
    bpy.ops.render.render(write_still=True)
