"""
turntable_scene_template.py -- builds a standard turntable rig inside
Blender: a 3-point light setup, a seamless ground/backdrop, and a camera.
The asset itself is spun 360 degrees rather than the camera, which keeps
lighting consistent across the whole render.

Only usable from inside a running Blender (imports bpy). Kept separate from
generate_turntable.py so the rig can be tuned/replaced independently of the
render/encode/publish logic.
"""
from __future__ import annotations

import math

import bpy


def _clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _add_three_point_lighting(target_size: float):
    dist = target_size * 3
    energy = target_size * 400

    key = bpy.data.lights.new(name="TT_Key", type="AREA")
    key.energy = energy
    key_obj = bpy.data.objects.new("TT_Key", key)
    key_obj.location = (dist, -dist, dist)
    key_obj.rotation_euler = (math.radians(55), 0, math.radians(45))
    bpy.context.collection.objects.link(key_obj)

    fill = bpy.data.lights.new(name="TT_Fill", type="AREA")
    fill.energy = energy * 0.4
    fill_obj = bpy.data.objects.new("TT_Fill", fill)
    fill_obj.location = (-dist, -dist * 0.6, dist * 0.5)
    fill_obj.rotation_euler = (math.radians(70), 0, math.radians(-40))
    bpy.context.collection.objects.link(fill_obj)

    rim = bpy.data.lights.new(name="TT_Rim", type="AREA")
    rim.energy = energy * 0.6
    rim_obj = bpy.data.objects.new("TT_Rim", rim)
    rim_obj.location = (0, dist, dist * 0.8)
    rim_obj.rotation_euler = (math.radians(-60), 0, 0)
    bpy.context.collection.objects.link(rim_obj)


def _add_ground(target_size: float):
    bpy.ops.mesh.primitive_plane_add(size=target_size * 10, location=(0, 0, 0))
    ground = bpy.context.active_object
    ground.name = "TT_Ground"

    mat = bpy.data.materials.new(name="TT_GroundMat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.18, 0.18, 0.18, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.85
    ground.data.materials.append(mat)
    return ground


def _add_camera(target_size: float, resolution):
    cam_data = bpy.data.cameras.new("TT_Camera")
    cam_data.lens = 50
    cam_obj = bpy.data.objects.new("TT_Camera", cam_data)
    cam_obj.location = (0, -target_size * 3.2, target_size * 1.1)
    cam_obj.rotation_euler = (math.radians(78), 0, 0)
    bpy.context.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj

    scene = bpy.context.scene
    scene.render.resolution_x = resolution[0]
    scene.render.resolution_y = resolution[1]
    return cam_obj


def _bounds_size(obj) -> float:
    """Rough bounding-sphere-ish size estimate across obj and its children,
    used to scale lights/camera/ground to the imported asset."""
    import mathutils

    coords = []

    def collect(o):
        for corner in o.bound_box:
            coords.append(o.matrix_world @ mathutils.Vector(corner))
        for child in o.children:
            collect(child)

    collect(obj)
    if not coords:
        return 2.0

    xs = [c.x for c in coords]
    ys = [c.y for c in coords]
    zs = [c.z for c in coords]
    size = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    return max(size, 0.1)


def build_turntable(
    asset_root_obj,
    frame_start: int,
    frame_end: int,
    turns: int,
    resolution,
    render_engine: str,
):
    """Given the already-imported asset's root object, build the lighting /
    ground / camera rig around it and keyframe a `turns`-revolution spin
    over [frame_start, frame_end]. Returns nothing; mutates the scene."""
    scene = bpy.context.scene
    scene.frame_start = frame_start
    scene.frame_end = frame_end
    scene.render.engine = render_engine

    size = _bounds_size(asset_root_obj)

    _add_ground(size)
    _add_three_point_lighting(size)
    _add_camera(size, resolution)

    asset_root_obj.rotation_mode = "XYZ"
    asset_root_obj.rotation_euler = (0, 0, 0)
    asset_root_obj.keyframe_insert(data_path="rotation_euler", index=2, frame=frame_start)
    asset_root_obj.rotation_euler = (0, 0, math.radians(360 * turns))
    asset_root_obj.keyframe_insert(data_path="rotation_euler", index=2, frame=frame_end)

    for fcurve in asset_root_obj.animation_data.action.fcurves:
        for kp in fcurve.keyframe_points:
            kp.interpolation = "LINEAR"

    # World background so unlit render regions aren't pure black.
    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.03, 0.03, 0.03, 1.0)
        bg.inputs["Strength"].default_value = 1.0
