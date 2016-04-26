from math import sin, cos, ceil, floor, pi
import importlib
import bpy
import mathutils
from os.path import join, exists
import os
from mathutils import Vector, Quaternion, Euler
from contextlib import contextmanager
from uuid import uuid4
from bpy.utils import register_module, unregister_module
from bpy import props as p
import json
import inspect
from collections import OrderedDict
import sys
from functools import partial
import tempfile
from pyhull.delaunay import DelaunayTri as Delaunay
import struct



bl_info = {
    "name": "Lightprobe",
    "description": "Gives ability to add light probes to a cycles render. \
Light probes sample incoming light at that location and generate 9 \
coefficients that can be used to quickly simluate that lighting in a real-time \
game engine.",
    "category": "Object",
    "author": "Andrew Moffat",
    "version": (1, 0),
    "blender": (2, 7, 1)
}


JSON_FILE_NAME = "lightprobes.json"
FAILSAFE_OFFSET = 0.00001
BAKE_SIZE = 32
CUBEMAP_EXTENSION = "cube"
CUBEMAP_FORMAT = "exr"

CUBEMAP_DIRECTION_LOOKUP = OrderedDict((
    ("posx", Quaternion((0.5, 0.5, -0.5, -0.5))),
    ("negx", Quaternion((0.5, 0.5, 0.5, 0.5))),
    ("posy", Quaternion((0, 0.0, -1.0, 0.0))),
    ("negy", Quaternion((0.0, 0.0, 0.0, -1.0))),
    ("posz", Quaternion((0, 0, -0.7071067690849304, -0.70710688829422))),
    ("negz", Quaternion((-0.70710688829422, -0.7071067690849304, 0, 0.0))),
))

# http://cseweb.ucsd.edu/~ravir/papers/envmap/envmap.pdf
spherical_harmonics = {
    (0, 0): lambda theta, phi: 0.282095,
    
    (1, -1): lambda theta, phi: 0.488603 * sin(theta) * sin(phi),
    (1, 0): lambda theta, phi: 0.488603 * cos(theta),
    (1, 1): lambda theta, phi: 0.488603 * sin(theta) * cos(phi),
    
    (2, -2): lambda theta, phi: 1.092548 * sin(theta) * cos(phi) * sin(theta) * sin(phi),
    (2, -1): lambda theta, phi: 1.092548 * sin(theta) * sin(phi) * cos(theta),
    (2, 0): lambda theta, phi: 0.315392 * (3 * cos(theta)**2 - 1),
    (2, 1): lambda theta, phi: 1.092548 * sin(theta) * cos(phi) * cos(theta),
    (2, 2): lambda theta, phi: 0.546274 * (((sin(theta) * cos(phi)) ** 2) - ((sin(theta) * sin(phi)) ** 2))
}



def is_lightprobe(ob):
    return ob.name.startswith("lightprobe-")

def is_cubemap(ob):
    return ob.name.startswith("cubemap_probe-")

def all_active_lightprobes():
    for ob in bpy.context.scene.objects:
        if is_lightprobe(ob):
            yield ob

def get_all_lightprobe_data():
    probe_data = []
    all_data = {
        "probes": probe_data,
    }

    scale_by = bpy.context.scene.unit_settings.scale_length
    
    for probe in all_active_lightprobes():
        coeffs = get_coeff_prop(probe)
        if not coeffs:
            continue

        data = {}
        data["loc"] = [p*scale_by for p in list(probe.location)]
        data["name"] = probe.lightprobe.name or None
        data["coeffs"] = coeffs

        probe_data.append(data)

    point_data = [d["loc"] for d in probe_data]
    tris = Delaunay(point_data)
    simplices = tris.vertices
    all_data["simplices"] = simplices


    # since we're using qhull now, and no longer scipy, we must construct our
    # neighbor structure manually
    neighbors = []
    for simp_idx, simp in enumerate(simplices):
        cur_neighbors = []
        neighbors.append(cur_neighbors)

        for vert_idx_idx, vert_idx in enumerate(simp):

            # construct a set of vert indices that we want to search the other
            # simplices for
            to_match = list(simp)
            to_match.pop(vert_idx_idx)
            to_match = set(to_match)

            neighbor = None

            for search_simp_idx, search_simp in enumerate(simplices):
                # skip the simplex that we're doing a search on
                if simp_idx == search_simp_idx:
                    continue

                try_match = set(search_simp)
                if to_match < try_match:
                    neighbor = search_simp_idx
                    break

            cur_neighbors.append(neighbor)

    all_data["neighbors"] = neighbors
    return all_data


@contextmanager
def values(values):
    restore_fns = []
    
    def create_restore(ob, name, value):
        def restore():
            setattr(ob, name, value)
        return restore
    
    for ob, ob_values in values.items():
        for name, value in ob_values.items():
            old_value = getattr(ob, name)
            setattr(ob, name, value)
            restore_fns.append(create_restore(ob, name, old_value))
            
    try:
        yield
    finally:
        for fn in restore_fns:
            try:
                fn()
            except:
                pass
            


def render_cubemap(ctx, h, ob, size, progress_update=None):
    scene = ctx.scene


    @contextmanager
    def temp_camera():
        bpy.ops.object.camera_add()
        cam = ctx.active_object
        
        cam.data.lens_unit = "FOV"
        cam.data.angle = pi/2
        cam.location = ob.location
        cam.rotation_mode = "QUATERNION"

        try:
            with no_interfere_ctx():
                yield cam
        finally:
            scene.objects.unlink(cam)


    filepaths = []
    name = ob.cubemap.name

    def render(direction):

        with temp_camera() as cam:
            cam.scale.x *= -1
            cam.rotation_quaternion = CUBEMAP_DIRECTION_LOOKUP[direction]

            filename = name + "-" + direction + ".exr"
            filepath = join(tempfile.gettempdir(), filename)

            with values({scene.render: {"resolution_x": size, "resolution_y": size,
                    "filepath": filepath}, scene: {"camera": cam},
                    scene.render.image_settings: {"file_format": "OPEN_EXR"},
                    ob: {"hide": True}}):
                bpy.ops.render.render(animation=False, write_still=True)

        return filepath
        

    for direction in CUBEMAP_DIRECTION_LOOKUP.keys():
        if progress_update:
            progress_update()
        filepath = render(direction)
        filepaths.append(filepath)
        
    bpy.ops.object.delete()

    # concatenate all of our directions together into a single env file
    # containing all 6 cube faces
    for filepath in filepaths:
        with open(filepath, "rb") as j:
            face = j.read()
            h.write(struct.pack("<I", len(face)))
            h.write(face)
        

def get_or_create_probe_file():
    if JSON_FILE_NAME not in bpy.data.texts:
        bpy.data.texts.new(JSON_FILE_NAME)
        
    f = bpy.data.texts[JSON_FILE_NAME]
    return f


def write_lightprobe_data(data):
    f = get_or_create_probe_file()
    f.clear()        
    data = json.dumps(data, indent=4, sort_keys=True)
    f.write(data)
    
    
def fetch_integration_callback(name):
    parts = name.split(".")
    fn_name = parts[-1]
    module_name = ".".join(parts[:-1])
    
    try:
        module = importlib.import_module(module_name)
    except:
        return None
    else:
        fn = getattr(module, fn_name, None)
        return fn
    
    
def pre_bake_hook(name, context, probe):
    fn = fetch_integration_callback(name)
    ret = None
    if fn:
        ret = fn(context, probe)
    return ret
    
def post_bake_hook(name, context, data, pre_bake_data):
    fn = fetch_integration_callback(name)
    if fn:
        fn(context, data, pre_bake_data)


def hide_object(ob):
    """ hides an object from cycles rendering, and returns a function that,
    when called, will restore the visibility """
    print("hiding object", ob) 
    old_value = ob.hide_render
    
    ob.hide_render = True    
    def restore():
        ob.hide_render = old_value
            
    return restore
        
        
def set_coeff_prop(ob, coeffs):
    """ sets our SH coeffs onto an object datablock.  we can't use the python
    dictionary that we've generated, so we'll flatten it to something that
    can be stored in a datablock """
        
    ob["lightprobe_coeffs"] = json.dumps(coeffs)
    
    
def get_coeff_prop(ob):
    """ retrieve our SH coeffs from our object datablock.  this is essentially
    unserializing it to our original data """
    
    coeffs = ob.get("lightprobe_coeffs", None)
    if not coeffs:
        return None
        
    data = json.loads(coeffs)
    return data
    
    


def setup_lightprobe_material(ob):
    scene = bpy.context.scene
    
    mat = bpy.data.materials.new(ob.name)
    mat.use_nodes = True
    
    tree = mat.node_tree
    
    for node in list(tree.nodes):
        tree.nodes.remove(node)
        
    diffuse = tree.nodes.new("ShaderNodeBsdfDiffuse")
    diffuse.inputs["Color"].default_value = (1, 1, 1, 1)
    diffuse.inputs["Roughness"].default_value = 1.0
    diffuse_out = diffuse.outputs["BSDF"]
    
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output_in = output.inputs["Surface"]
    
    tree.links.new(diffuse_out, output_in)
    
    bake_node = tree.nodes.new("ShaderNodeTexImage")
    bake_node.label = bake_node.name
    bake_out = bake_node.outputs["Color"]
    
    texture = create_lightmap_image(ob, BAKE_SIZE, BAKE_SIZE)
    bake_node.image = texture
    
    ob.data.uv_textures["lightmap"].active = True
    
    color_uvmap = tree.nodes.new("ShaderNodeUVMap")
    color_uvmap.uv_map = "lightmap"
    tree.links.new(color_uvmap.outputs["UV"], bake_node.inputs["Vector"])
    
    ob.data.materials.append(mat)
        

@contextmanager
def no_interfere_ctx():
    """ allows us to perform operations without affecting our selected or active
    objects """
    ctx = bpy.context
    old_selected_objects = ctx.selected_objects
    active_object = ctx.active_object
    try:
        yield
    finally:
        for obj in ctx.selected_objects:
            obj.select = False
        for obj in old_selected_objects:
            if obj.name in bpy.data.objects:
                obj.select = True

        if active_object and active_object.name in bpy.data.objects:
            ctx.scene.objects.active = active_object

@contextmanager
def active_and_selected(ob):
    ctx = bpy.context
    with selected(ob):
        ctx.scene.objects.active = ob
        yield

def deselect(ctx):
    for obj in ctx.selected_objects:
        obj.select = False

@contextmanager
def selected(obs):
    ctx = bpy.context
    with no_interfere_ctx():
        deselect(ctx)
        if not isinstance(obs, (list, tuple)):
            obs = [obs]
        for ob in obs:
            ob.select = True
        yield
    
def hide_all(scene):
    restores = {}
    for ob in scene.objects:
        restore = hide_object(ob)
        restores[ob] = restore
    return restores


def override_ctx(**kwargs):
    ctx = bpy.context.copy()
    ctx.update(kwargs)
    return ctx


def create_lightmap_image(ob, width, height):
    name = ob.name
    bpy.ops.image.new(override_ctx(object=ob), name=name, width=width,
            height=height, alpha=False, float=True)
    return bpy.data.images[name]

def get_lightmap(ob):
    return bpy.data.images[ob.name]


def add_lightprobe():
    with no_interfere_ctx():
        bpy.ops.mesh.primitive_cube_add()
        probe = bpy.context.object
        
        for _ in range(len(probe.data.uv_layers)):
            bpy.ops.mesh.uv_texture_remove()
        
        bpy.ops.mesh.uv_texture_add()
        probe.data.uv_layers[0].name = "lightmap"
        
        bpy.ops.uv.lightmap_pack(PREF_CONTEXT="ALL_FACES",
                PREF_PACK_IN_ONE=True, PREF_NEW_UVLAYER=False,
                PREF_APPLY_IMAGE=False, PREF_IMG_PX_SIZE=512, PREF_BOX_DIV=12,
                PREF_MARGIN_DIV=0.1)
        
        bpy.ops.object.modifier_add(type="SUBSURF")
        bpy.context.object.modifiers["Subsurf"].levels = 4
        
        bpy.ops.object.modifier_add(type="TRIANGULATE")
        bpy.ops.object.convert(target='MESH')

    
        probe.scale = mathutils.Vector((0.3, 0.3, 0.3))
        bpy.ops.object.shade_smooth()
        
        
    probe.name = "lightprobe-" + uuid4().hex
    return probe


def add_cubemap_probe():
    with no_interfere_ctx():
        bpy.ops.mesh.primitive_cube_add()
        probe = bpy.context.object
        
        for _ in range(len(probe.data.uv_layers)):
            bpy.ops.mesh.uv_texture_remove()
        
        bpy.ops.mesh.uv_texture_add()
        probe.data.uv_layers[0].name = "lightmap"
        
        bpy.ops.uv.lightmap_pack(PREF_CONTEXT="ALL_FACES",
                PREF_PACK_IN_ONE=True, PREF_NEW_UVLAYER=False,
                PREF_APPLY_IMAGE=False, PREF_IMG_PX_SIZE=512, PREF_BOX_DIV=12,
                PREF_MARGIN_DIV=0.1)
        
        probe.scale = mathutils.Vector((0.3, 0.3, 0.3))
        
    probe.name = "cubemap_probe-" + uuid4().hex
    hide_object(probe)
    return probe    


def bake(ob):
    with active_and_selected(ob):
        scene = bpy.context.scene

        cycles = scene.cycles
        old_samples = cycles.samples
        
        cycles.samples = scene.lightprobe.samples        
        
        bpy.ops.object.bake(type="COMBINED")
        cycles.samples = old_samples



def get_lightprobe_coefficients(probe, theta_res, phi_res):
    probe.data.calc_tessface()
    bake(probe)
    lightmap = get_lightmap(probe)
    return get_all_coefficients(probe, lightmap, theta_res, phi_res)




def sample_image(channels, width, height, pixel_data, loc):
    """ samples a blender location at a particular xy integer location """    
    x, y = loc[0], loc[1]
    
    pix_loc = int((y * width * channels) + x * channels)
    r = pixel_data[pix_loc + 0]
    g = pixel_data[pix_loc + 1]
    b = pixel_data[pix_loc + 2]
     
    return mathutils.Color((r, g, b))
    
    

def bilinear_interpolate(image, uv):
    """ performs bilinear interpolation of a blender image using texture-space
    uv coordinates.  the boundary conditions are to extend the edges """
    
    lightmap_size = mathutils.Vector(image.size)
    width, height = lightmap_size[0], lightmap_size[1]
    
    px_x, px_y = 1.0/width, 1.0/height
    half_px_x, half_px_y = 1.0/(2*width), 1.0/(2*height)
    
    left_coord = ceil(width * (uv[0] - half_px_x) - 1)
    right_coord = ceil(width * (uv[0] + half_px_x) - 1)
    bottom_coord = ceil(height * (uv[1] - half_px_y) - 1)
    top_coord = ceil(height * (uv[1] + half_px_y) - 1)
    
    
    # these are asking how much of 1-pixel (in uv space) has our uv coordinate
    # traversed, starting at the left/bottom pixel boundary
    lerp_x = (uv[0] - (left_coord + 0.5) / width) / px_x
    lerp_y = (uv[1] - (bottom_coord + 0.5) / height) / px_y
    
    
    # boundary conditions
    if right_coord + 1 > width:
        right_coord = left_coord
    
    if left_coord < 0:
        left_coord = right_coord
        
    if top_coord + 1 > height:
        top_coord = bottom_coord
        
    if bottom_coord < 0:
        bottom_coord = top_coord
        
    
    ll_uv = mathutils.Vector((left_coord, bottom_coord))
    lr_uv = mathutils.Vector((right_coord, bottom_coord))
    ur_uv = mathutils.Vector((right_coord, top_coord))
    ul_uv = mathutils.Vector((left_coord, top_coord))
    
    pixel_data = image.pixels[:]
    chan = image.channels
    width, height = image.size
    
    lower_left = mathutils.Vector(sample_image(chan, width, height, pixel_data, ll_uv))
    lower_right = mathutils.Vector(sample_image(chan, width, height, pixel_data, lr_uv))
    upper_right = mathutils.Vector(sample_image(chan, width, height, pixel_data, ur_uv))
    upper_left = mathutils.Vector(sample_image(chan, width, height, pixel_data, ul_uv))
    
    del pixel_data
    
    top = upper_left.lerp(upper_right, lerp_x)
    bottom = lower_left.lerp(lower_right, lerp_x)
    color = mathutils.Color(bottom.lerp(top, lerp_y))
    
    return color




# http://en.wikipedia.org/wiki/M%C3%B6ller%E2%80%93Trumbore_intersection_algorithm
def triangle_intersection(v1, v2, v3, ray, origin):
    """ performs moller-trumbore ray-triangle intersection and returns
    barycentric coordinates if an intersection exists, None otherwise """
    
    epsilon = 0.000001
    edge1 = v2 - v1
    edge2 = v3 - v1
    
    P = ray.cross(edge2)
    det = edge1.dot(P)
    
    if det > -epsilon and det < epsilon:
        return None
    
    inv_det = 1.0 / det
    T = origin - v1
    
    u = T.dot(P) * inv_det
    
    if u < 0 or u > 1:
        return None
    
    Q = T.cross(edge1)
    v = ray.dot(Q) * inv_det
    
    if v < 0 or u + v > 1:
        return None
    
    t = edge2.dot(Q) * inv_det
    
    if t > epsilon:
        w = 1 - u - v
        return mathutils.Vector((u, v, w))
    
    return None





    

def get_glsl_coefficients(coeffs):
    """ a convenience function for testing SH coefficients in the shader
    provided by the opengl orange book, second edition """
    
    tmpl = "const vec3 L%d%s%d = vec3(%f, %f, %f);"
    lines = []
    
    for l, mdata in coeffs.items():
        for m, color in mdata.items():
            
            sign = ""
            if m < 0:
                sign = "m"
                
            line = tmpl % (l, sign, abs(m), color[0], color[1], color[2])
            lines.append(line)
        
    return "\n".join(lines)
    

def get_all_coefficients(ob, lightmap, theta_res, phi_res):
    """ returns all SH coefficients.  theta_res and phi_res are the
    sampling resolutions for theta (zenith) and phi (azimuth) respectively.
    theta ranges from 0-pi, while phi ranges from 0-2pi """
    mapping = {}
    for l, m in spherical_harmonics.keys():
        color = get_coefficients(ob, lightmap, l, m, theta_res, phi_res)
        mapping.setdefault(l, {})[m] = color
    return mapping


def get_coefficients(ob, lightmap, l, m, theta_res, phi_res):
    """ returns the RGB spherical harmonic coefficients for a given
    l and m """
    c = mathutils.Color((0, 0, 0))
    harmonic = spherical_harmonics[(l, m)]
    
    for theta in (pi * y / float(theta_res) for y in range(theta_res)):
        for phi in (pi * 2 * x / float(phi_res) for x in range(phi_res)):
            color = sample_icosphere_color(ob, lightmap, theta, phi)
            c += (color * harmonic(theta, phi) * sin(theta)
                / (theta_res * phi_res))
            
    return c.r, c.g, c.b
            
def sample_icosphere_color(ob, lightmap, theta, phi):
    """ takes a theta and phi and casts a ray out from the center of an
    icosphere, bilinearly sampling the surface where the ray intersects """
    ray = angle_to_ray(theta, phi)
    
    # we extend the ray arbitrarily so it's guaranteed to intersect with the
    # icosphere, instead of falling short
    ray *= 100
    
    face, location = find_intersecting_face(ob, ray)
    
    # it is possible that we couldn't find an intersecting face if the ray
    # we shot aligns perfectly with a vertex.  in this case, we'll offset the
    # ray slightly and try again.  this should not fail a second time.
    if face is None:
        ray.x += FAILSAFE_OFFSET
        ray.y += FAILSAFE_OFFSET
        ray.z += FAILSAFE_OFFSET
        
        face, location = find_intersecting_face(ob, ray)
        assert(face is not None)
    
    color = sample_lightmap(ob, lightmap, face, location)
    return color

    
def angle_to_ray(theta, phi):
    """ converts a spherical coordinate to cartesian coordinate """
    x = sin(theta) * cos(phi)
    y = sin(theta) * sin(phi)
    z = cos(theta)
    ray = mathutils.Vector((x, y, z)).normalized()
    return ray


def find_intersecting_face(ob, ray):
    """ finds the face where a ray from the center of an icosphere
    intersects """
    mesh = ob.data
    origin = mathutils.Vector()
    
    # we'll use scale to ensure that our transform applies to our vertices.
    # assume scale is uniform and just use x scale
    scale = ob.scale[0]
    
    for face in mesh.tessfaces:
        v = face.vertices
        v1 = mesh.vertices[v[0]].co * scale
        v2 = mesh.vertices[v[1]].co * scale
        v3 = mesh.vertices[v[2]].co * scale
        
        intersection = triangle_intersection(v1, v2, v3, ray, origin)
        if intersection:
            return face, intersection
        
    # we should never get here, but we may in the case of a ray aligning
    # perfectly with a vertex.  in this case, we'll catch this error up at
    # the caller
    return None, None
    
        
def sample_lightmap(ob, lightmap, face, loc):
    """ """
    mesh = ob.data
    uvs = mesh.tessface_uv_textures[0].data[face.index]
    location_uv = loc[0] * uvs.uv1 + loc[1] * uvs.uv2 + loc[2] * uvs.uv3
    
    return bilinear_interpolate(lightmap, location_uv)
    
    
    
    
class LightProbeConfigPanel(bpy.types.Panel):
    bl_label = "Light Probe"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "scene"
    
    def draw(self, context):
        layout = self.layout
        
        scene = context.scene
        
        # TODO: i would like to make this a search dropdown that will
        # autocomplete the operator name
        row = layout.row()
        name = scene.lightprobe.pre_bake_hook
        fn = fetch_integration_callback(name)
        row.alert = bool(name and fn is None)
        row.prop(scene.lightprobe, "pre_bake_hook")
        
        row = layout.row()
        name = scene.lightprobe.post_bake_hook
        fn = fetch_integration_callback(name)
        row.alert = bool(name and fn is None)
        row.prop(scene.lightprobe, "post_bake_hook")
        
        layout.prop(scene.lightprobe, "cubemap_dir")
        
        row = layout.row()
        row.prop(scene.lightprobe, "theta_res")
        row.prop(scene.lightprobe, "phi_res")
        
        layout.prop(scene.lightprobe, "samples")

        layout.operator(BakeAllOperator.bl_idname)
        layout.operator(ResizeAllOperator.bl_idname)
    
    
    
class CubemapProbePanel(bpy.types.Panel):
    bl_label = "Cubemap Probe"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    
    @classmethod
    def poll(self, context):
        return is_cubemap(context.object)
    
    def draw(self, context):
        ob = context.object
        layout = self.layout
        c = ob.cubemap
        
        layout.prop(c, "name")
        layout.prop(c, "sky_only")
        layout.prop(c, "size")

        can_set_range = not c.single_frame and not c.whole_range

        col = layout.column()
        col.enabled = can_set_range

        row = col.row()
        row.alert = c.start_frame > c.end_frame

        row.prop(c, "start_frame")
        row.prop(c, "end_frame")

        row = layout.row()
        row.prop(c, "fps")

        row = layout.row()
        col = row.column()
        col.enabled = not c.whole_range
        col.prop(c, "single_frame")

        col = row.column()
        col.enabled = not c.single_frame
        col.prop(c, "whole_range")

        layout.operator(BakeCubemapOperator.bl_idname)
    
    
class LightProbePanel(bpy.types.Panel):
    bl_label = "Light Probe"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    
    @classmethod
    def poll(self, context):
        return is_lightprobe(context.object)

    def draw(self, context):
        layout = self.layout

        ob = context.object
        lp = ob.lightprobe
        
        row = layout.row()
        row.prop(lp, "name")
        
        layout.operator(BakeOperator.bl_idname)


class BakeCubemapOperator(bpy.types.Operator):
    bl_idname = "object.bake_cubemap"
    bl_label = "Bake Cube Map"
    
    @classmethod
    def poll(cls, context):
        cycles = context.scene.render.engine == "CYCLES"
        has_name = context.object.cubemap.name
        has_dir = context.scene.lightprobe.cubemap_dir
        return cycles and has_name and has_dir

    def modal(self, ctx, event):
        ret = {"PASS_THROUGH"}

        if event.type == "TIMER":
            try:
                next(self.next_chunk)
            except StopIteration:
                ret = {"FINISHED"}

        elif event.type in {"ESC", "RIGHTMOUSE"}:
            self.cancel(ctx)
            ret = {"CANCELLED"}

        return ret

    def cancel(self, ctx):
        wm = ctx.window_manager
        wm.event_timer_remove(self._timer)
    
    def execute(self, ctx):
        probe = ctx.object
        scene = ctx.scene
        cube = probe.cubemap
        size = cube.size
        
        cubemap_dir = bpy.path.abspath(scene.lightprobe.cubemap_dir)

        def update_gen(num_frames):
            wm = ctx.window_manager
            wm.progress_begin(0, (6*num_frames)-1)
            
            for i in range(6*num_frames):
                wm.progress_update(i)
                yield


        if cube.single_frame:
            start = scene.frame_current
            end = start
        elif cube.whole_range:
            start = scene.frame_start
            end = scene.frame_end
        else:
            start = cube.start_frame
            end = cube.end_frame


        fps = scene.render.fps
        target_fps = cube.fps
        frame_advance = fps / target_fps


        # figure out all the frames we actually need to render, given our custom
        # fps
        all_frames = [start]

        cur_frame = start
        last_frame = None
        while cur_frame < end:
            cur_frame = cur_frame + frame_advance
            if int(cur_frame) == last_frame:
                continue

            all_frames.append(int(cur_frame))
            last_frame = int(cur_frame)


        awesome = update_gen(len(all_frames))
        update_fn = lambda: next(awesome)


        gamma = 1.0

        cubemap_out_name = "%s.%s" % (cube.name, CUBEMAP_EXTENSION)
        cubemap_filename = join(cubemap_dir, cubemap_out_name)

        out_handle = open(cubemap_filename, "wb")
        out_handle.write(struct.pack("<ffI", fps, gamma, len(all_frames)))

        def fn():
            with no_interfere_ctx():
                restores = {
                    probe: hide_object(probe),
                }
                if cube.sky_only:
                    restores = hide_all(scene)
                restores[probe]()

                # for each frame that this cubemap is set to render for, render all
                # six sides of the cube map
                try:
                    for frame in all_frames:
                        scene.frame_set(frame)
                        render_cubemap(ctx, out_handle, probe, size, update_fn)
                        yield

                finally:
                    for restore in restores.values():
                        restore()


        self.next_chunk = fn()
                    
        wm = ctx.window_manager
        self._timer = wm.event_timer_add(0.5, ctx.window)
        wm.modal_handler_add(self)

        return {"RUNNING_MODAL"}
    
        
class BakeOperator(bpy.types.Operator):
    bl_idname = "object.bake_lightprobe"
    bl_label = "Bake Light Probe"
    
    @classmethod
    def poll(cls, context):
        cycles = context.scene.render.engine == "CYCLES"
        return cycles

    def execute(self, context):
        probe = context.active_object
        scene = context.scene
        
        settings = scene.lightprobe
        coeffs = get_lightprobe_coefficients(probe, settings.theta_res,
                settings.phi_res)
        set_coeff_prop(probe, coeffs)
        
        return {"FINISHED"}
    
    
class BakeAllOperator(bpy.types.Operator):
    bl_idname = "object.bake_all_lightprobes"
    bl_label = "Bake All Light Probes"

    def execute(self, context):
        scene_settings = context.scene.lightprobe

        all_probes = all_active_lightprobes()
        ret = pre_bake_hook(scene_settings.pre_bake_hook, context, all_probes)
        
        for probe in all_probes:
            with active_and_selected(probe):
                bpy.ops.object.bake_lightprobe()
        
        lp_data = get_all_lightprobe_data()
        write_lightprobe_data(lp_data)
        post_bake_hook(scene_settings.post_bake_hook, context, lp_data, ret)


        return {"FINISHED"}
    
    
class ResizeAllOperator(bpy.types.Operator):
    bl_idname = "object.resize_all_lightprobes"
    bl_label = "Resize Light Probes"
    bl_options = {"REGISTER", "UNDO"}
    
    size = p.FloatProperty(name="Units", default=1)
    
    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        dimensions = mathutils.Vector((self.size, self.size, self.size))
        
        for probe in all_active_lightprobes():
            probe.dimensions = dimensions
        
        return {"FINISHED"}


class LightProbeOperator(bpy.types.Operator):
    bl_idname = "object.add_lightprobe"
    bl_label = "Add Light Probe"
    
    def execute(self, context):
        probe = add_lightprobe()
        hide_object(probe)
        probe.show_x_ray = True
        
        with active_and_selected(probe):
            setup_lightprobe_material(probe)
            
        return {"FINISHED"}
    
    
class AddCubemapOperator(bpy.types.Operator):
    bl_idname = "object.add_cubemap_probe"
    bl_label = "Add Cubemap Probe"
    
    def execute(self, context):
        probe = add_cubemap_probe()
        return {"FINISHED"}



def get_field(field, default=None):
    """ if we use a setter to set a property on an object, sometimes we need a
    corresponding getter.  it doesn't seem to be needed in the case of a raw
    property on the ID object, but if you use PointerProperty to a
    PropertyGroup, blender has trouble drilling into the PropertyGroup for the
    ID property, so we need to stupid function to get it """
    def wrapper(self):
        return self.get(field, default)
    return wrapper


def make_validator(fn, field, default=None):
    def wrapper(self, value):
        last_value = self.get(field, default)
        new_value = fn(field, last_value, self, value)
        self[field] = new_value
    return wrapper


def validate_max_frame(field, old_value, self, value):
    ctx = bpy.context
    max_frame = ctx.scene.frame_end
    return min(value, max_frame)

def validate_min_frame(field, old_value, self, value):
    ctx = bpy.context
    min_frame = ctx.scene.frame_start
    return max(value, min_frame)


class SceneProperties(bpy.types.PropertyGroup):
    pre_bake_hook = p.StringProperty(name="Pre-bake hook")
    post_bake_hook = p.StringProperty(name="Post-bake hook", description="""Call \
this function with lightprobe data.  Used for integrating with other plugins.""")
    cubemap_dir = p.StringProperty(name="Cubemap Directory", default="", subtype="DIR_PATH")
    theta_res = p.IntProperty(name="Theta Samples", default=10)
    phi_res = p.IntProperty(name="Phi Samples", default=20)
    samples = p.IntProperty(name="Bake samples", default=50)
    
class ProbeProperties(bpy.types.PropertyGroup):
    name = p.StringProperty(name="Probe Name", default="")
    
class CubemapProperties(bpy.types.PropertyGroup):
    name = p.StringProperty(name="Probe Name", default="")
    size = p.IntProperty(name="Size", default=256)
    sky_only = p.BoolProperty(name="Sky only", default=False)

    start_frame = p.IntProperty(name="Start Frame", subtype="UNSIGNED",
            set=make_validator(validate_min_frame, "start_frame"),
            get=get_field("start_frame", 1))
    end_frame = p.IntProperty(name="End Frame", subtype="UNSIGNED", 
            set=make_validator(validate_max_frame, "end_frame"),
            get=get_field("end_frame", 1))
    single_frame = p.BoolProperty(name="Just this frame")
    whole_range = p.BoolProperty(name="Entire range")
    fps = p.FloatProperty(name="Framerate", default=30.0)
    
    
def register():
    register_module(__name__)
    bpy.types.Object.cubemap = p.PointerProperty(type=CubemapProperties)
    bpy.types.Object.lightprobe = p.PointerProperty(type=ProbeProperties)
    bpy.types.Scene.lightprobe = p.PointerProperty(type=SceneProperties)



def unregister():
    unregister_module(__name__)
    del bpy.types.Object.lightprobe
    del bpy.types.Scene.lightprobe
    
try:
    unregister()
except:
    pass
register()

