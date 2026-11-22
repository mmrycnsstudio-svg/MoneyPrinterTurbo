"""Create a tiny CPU-rendered animation for installation verification."""
import argparse
import math
from pathlib import Path
import sys

import bpy

parser = argparse.ArgumentParser()
parser.add_argument('--output-dir', type=Path, required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
args.output_dir.mkdir(parents=True, exist_ok=True)

scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
scene.cycles.samples = 4
scene.cycles.use_denoising = False
scene.render.threads_mode = 'FIXED'
scene.render.threads = 2
scene.render.resolution_x = 180
scene.render.resolution_y = 320
scene.render.resolution_percentage = 100
scene.render.fps = 8
scene.render.image_settings.file_format = 'PNG'
scene.render.filepath = str(args.output_dir / 'frame-')
scene.frame_start = 1
scene.frame_end = 8

cube = bpy.data.objects['Cube']
material = bpy.data.materials.new('Demo Green')
material.diffuse_color = (0.08, 0.65, 0.38, 1)
cube.data.materials.append(material)
cube.rotation_euler.z = 0
cube.keyframe_insert(data_path='rotation_euler', frame=1)
cube.rotation_euler.z = math.pi / 2
cube.keyframe_insert(data_path='rotation_euler', frame=8)
scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=str(args.output_dir / 'blender-smoke.blend'))
bpy.ops.render.render(animation=True)
