import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import to_hex
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

PKG = Path(__file__).parent
urdf = ET.parse(PKG / "urdf" / "hu501urdf.urdf").getroot()


def rpy_to_R(rpy):
    r, p, y = rpy
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    return Rz @ Ry @ Rx


def load_stl(path):
    data = np.fromfile(path, dtype=np.uint8)
    n = struct.unpack("<I", data[80:84].tobytes())[0]
    rec = np.dtype([("normal", "<f4", 3), ("verts", "<f4", (3, 3)), ("attr", "<u2")])
    tri = np.frombuffer(data[84:84 + 50 * n].tobytes(), dtype=rec)
    return tri["verts"].astype(np.float64)


links = {}
for link in urdf.findall("link"):
    name = link.get("name")
    vis = link.find("visual")
    if vis is None:
        continue
    mesh = vis.find("geometry/mesh").get("filename")
    rgba = vis.find("material/color").get("rgba").split()
    o = vis.find("origin")
    Tv = np.eye(4)
    Tv[:3, 3] = np.array(o.get("xyz").split(), dtype=float) if o.get("xyz") else np.zeros(3)
    Tv[:3, :3] = rpy_to_R(np.array(o.get("rpy").split(), dtype=float)) if o.get("rpy") else np.eye(3)
    links[name] = (mesh, np.array(rgba, dtype=float), Tv)

joints = []  # (parent, child, T)
for j in urdf.findall("joint"):
    o = j.find("origin")
    xyz = np.array(o.get("xyz").split(), dtype=float) if o.get("xyz") else np.zeros(3)
    rpy = np.array(o.get("rpy").split(), dtype=float) if o.get("rpy") else np.zeros(3)
    T = np.eye(4)
    T[:3, :3] = rpy_to_R(rpy)
    T[:3, 3] = xyz
    joints.append((j.find("parent").get("link"), j.find("child").get("link"), T))

world_T = {"base_link": np.eye(4)}
while len(world_T) < 1 + len(joints):
    for p, c, T in joints:
        if p in world_T and c not in world_T:
            world_T[c] = world_T[p] @ T

fig = plt.figure(figsize=(14, 10), facecolor="white")
ax = fig.add_subplot(111, projection="3d")

all_pts = []
for name, (mesh, rgba, Tv) in links.items():
    stl = PKG / "meshes" / Path(mesh).name
    verts = load_stl(stl)
    T = world_T[name] @ Tv
    flat = verts.reshape(-1, 3) @ T[:3, :3].T + T[:3, 3]
    tris = flat.reshape(-1, 3, 3)
    all_pts.append(flat)

    tri_normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lens = np.linalg.norm(tri_normals, axis=1)
    view = np.array([0.4, -0.6, 0.7])
    facing = (tri_normals @ view) < 0 if False else None

    shade = np.clip(np.abs(tri_normals @ view) / np.maximum(lens, 1e-12) * 0.7 + 0.35, 0, 1)
    base = rgba[:3] * 0.65 + 0.05
    colors = np.clip(shade[:, None] * base[None, :], 0, 1)

    alpha = 0.12 if name == "base_link" else 1.0
    pc = Poly3DCollection(tris, facecolors=colors, edgecolors="none", alpha=alpha)
    ax.add_collection3d(pc)

    origin = T[:3, 3]
    ax.scatter(*origin, color="red", s=40, depthshade=False)
    ax.text(*origin, f"  {name}", fontsize=8)

all_pts = np.vstack(all_pts)
lo, hi = all_pts.min(0), all_pts.max(0)
ctr, span = (lo + hi) / 2, (hi - lo).max() / 2 * 1.05
ax.set_xlim(ctr[0] - span, ctr[0] + span)
ax.set_ylim(ctr[1] - span, ctr[1] + span)
ax.set_zlim(ctr[2] - span, ctr[2] + span)
ax.set_box_aspect([1, 1, 1])
ax.set_xlabel("X (m)")
ax.set_ylabel("Y (m)")
ax.set_zlabel("Z (m)")

L = 0.4
for vec, c, lab in [([1,0,0],"red","X"), ([0,1,0],"green","Y"), ([0,0,1],"blue","Z")]:
    ax.quiver(0,0,0, *(np.array(vec)*L), color=c, lw=3, arrow_length_ratio=0.15, zorder=100)
    ax.text(*(np.array(vec)*L*1.15), lab, color=c, fontsize=11, fontweight="bold", zorder=100)
com = world_T["base_link"][:3,3] + world_T["base_link"][:3,:3] @ np.array([0,0,-0.16302])
ax.scatter(*com, color="magenta", marker="*",s=120, depthshade=False)
ax.text(*com, "  COM", color="magenta", fontsize=9)
ax.set_title("hu501urdf  (base_link + 4 thrusters, units as exported)")
ax.view_init(elev=25, azim=-60)

plt.tight_layout()
plt.savefig(PKG / "urdf_view.png", dpi=130)
print("saved urdf_view.png")
print("bounds min:", lo.round(4), "max:", hi.round(4))
