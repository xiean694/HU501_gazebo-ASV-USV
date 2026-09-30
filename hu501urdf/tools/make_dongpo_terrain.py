#!/usr/bin/env python3
# 生成东坡湖地形素材（新版，基于高清卫星图直接分割）
#   高清卫星图(东坡湖_卫星地图.jpg)中湖水为蓝色(B-R>35)，可直接像素分割，
#   不再需要 2D 地图配准与目视锚点。
#   产物: 高度图 PNG + 地形网格 OBJ + 卫星贴图 JPG + 元数据 JSON
# 用法: python3 hu501urdf/tools/make_dongpo_terrain.py   (改 CONFIG 后重跑即可)
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

TOOLS = Path(__file__).resolve().parent
PKG = TOOLS.parent                      # hu501urdf/
WS = PKG.parent                         # 工作区根目录
TEX_DIR = PKG / 'models' / 'dongpo_lake_terrain' / 'materials' / 'textures'

CONFIG = dict(
    sat_img=WS / '东坡湖_卫星地图.jpg',   # 高清卫星图(蓝色水面)
    lake_length_m=473.06,               # 实测湖长(比例标定)
    lake_width_m=275.81,                # 实测湖宽(交叉校验)
    out_res=1024,                       # 碰撞高度图边长(过大导致 bullet 高度场构建卡死)
    tex_res=2048,                       # 卫星贴图边长
    land_h=2.5,                         # 陆地高程 [m] (水面 z=0)
    bed_h=-3.5,                         # 湖盆高程 [m]
    water_br_min=35,                    # 水色判据: B-R 下限
    water_b_min=80,                     # 水色判据: B 下限
    water_v_max=150,                    # 水色判据: HSV 亮度上限
    dike_px=110,                        # 四周堤岸环带宽(像素, ~24 m)
    min_island_px=3000,                 # 小于该面积的湖中洞当噪声填成水
    shore_band_px=14,                   # 岸带补采宽度: 近岸浅色水补进水体
    shore_br_min=20,                    # 岸带内放宽的水色阈值(B-R 下限)
    shore_std_max=6.0,                  # 岸带内平滑度上限(排除建筑/屋顶)
    shore_blur=5.0,                     # 岸坡平滑 σ(像素), 越小岸线越贴图
    mesh_step_px=6,                     # 地形网格采样步长(像素)
    spawn_margin_px=40,                 # 出生点离岸/离岛安全裕度(像素)
)


def components(mask):
    """所有连通域 [(y,x)列表, ...]（BFS, 无 scipy 依赖）"""
    H, W = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    comps = []
    for sy in range(H):
        for sx in range(W):
            if mask[sy, sx] and not visited[sy, sx]:
                stack, comp = [(sy, sx)], []
                visited[sy, sx] = True
                while stack:
                    y, x = stack.pop()
                    comp.append((y, x))
                    for ny, nx in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
                        if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] \
                           and not visited[ny, nx]:
                            visited[ny, nx] = True
                            stack.append((ny, nx))
                comps.append(comp)
    return comps


def largest_component(mask):
    comps = components(mask)
    out = np.zeros_like(mask)
    if comps:
        ys, xs = zip(*max(comps, key=len))
        out[list(ys), list(xs)] = True
    return out


def fill_enclosed_holes(mask):
    """被 mask 包围的洞(湖中岛)"""
    H, W = mask.shape
    outside = np.zeros_like(mask, dtype=bool)
    stack = []
    for x in range(W):
        for y in (0, H - 1):
            if not mask[y, x] and not outside[y, x]:
                outside[y, x] = True
                stack.append((y, x))
    for y in range(H):
        for x in (0, W - 1):
            if not mask[y, x] and not outside[y, x]:
                outside[y, x] = True
                stack.append((y, x))
    while stack:
        y, x = stack.pop()
        for ny, nx in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
            if 0 <= ny < H and 0 <= nx < W and not mask[ny, nx] \
               and not outside[ny, nx]:
                outside[ny, nx] = True
                stack.append((ny, nx))
    return (~mask) & (~outside)


def rolling_std(a, k=15):
    """15x15 滚动 RGB 标准差(积分图实现)。水面平滑(std小), 建筑有纹理(std大)"""
    H, W, _ = a.shape
    cum = np.zeros((H + 1, W + 1, 3))
    cum[1:, 1:] = np.cumsum(np.cumsum(a, 0), 1)
    cum2 = np.zeros((H + 1, W + 1, 3))
    cum2[1:, 1:] = np.cumsum(np.cumsum(a * a, 0), 1)
    ys, xs = np.mgrid[0:H - k, 0:W - k]
    n = k * k
    s1 = cum[ys + k, xs + k] - cum[ys, xs + k] - cum[ys + k, xs] + cum[ys, xs]
    s2 = cum2[ys + k, xs + k] - cum2[ys, xs + k] - cum2[ys + k, xs] \
        + cum2[ys, xs]
    std = np.sqrt(np.maximum(s2 / n - (s1 / n) ** 2, 0)).mean(-1)
    return np.pad(std, ((k // 2, k - k // 2), (k // 2, k - k // 2)),
                  mode='edge')


def palm_links(x, y, z, rng, uid=""):
    """棕榈树(纯 SDF 基元): 弯干 + 顶部放射状叶片"""
    h = 5.4 + rng.uniform(-0.9, 1.2)
    lean = rng.uniform(-0.12, 0.12)
    parts = [f'''    <visual name="t{uid}" cast_shadows="true">
      <pose>{x} {y} {z+h/2} 0 {lean} {rng.uniform(0, 6.28)}</pose>
      <geometry><cylinder><radius>0.09</radius><length>{h:.2f}</length></cylinder></geometry>
      <material><ambient>0.35 0.27 0.16</ambient><diffuse>0.42 0.33 0.2</diffuse></material>
    </visual>''']
    tx = x - math.sin(lean) * h * 0.5
    ty = y
    for k in range(7):
        yaw = k * 6.2832 / 7 + rng.uniform(-0.2, 0.2)
        parts.append(f'''    <visual name="f{uid}_{k}" cast_shadows="true">
      <pose>{tx:.2f} {ty:.2f} {z+h-0.1} 0 {-0.55} {yaw:.2f}</pose>
      <geometry><box><size>0.5 2.4 0.05</size></box></geometry>
      <material><ambient>0.08 0.35 0.1</ambient><diffuse>0.15 0.58 0.14</diffuse></material>
    </visual>''')
    return parts


def round_tree_links(x, y, z, rng, uid=""):
    """阔叶树: 直干 + 双球冠"""
    h = 3.2 + rng.uniform(-0.6, 1.0)
    r = 2.4 + rng.uniform(-0.5, 0.9)
    return [f'''    <visual name="t{uid}" cast_shadows="true">
      <pose>{x} {y} {z+h/2} 0 0 {rng.uniform(0, 6.28)}</pose>
      <geometry><cylinder><radius>0.14</radius><length>{h:.2f}</length></cylinder></geometry>
      <material><ambient>0.3 0.22 0.14</ambient><diffuse>0.36 0.27 0.17</diffuse></material>
    </visual>''',
            f'''    <visual name="c{uid}" cast_shadows="true">
      <pose>{x:.2f} {y:.2f} {z+h+r*0.55:.2f} 0 0 0</pose>
      <geometry><sphere><radius>{r:.2f}</radius></sphere></geometry>
      <material><ambient>0.12 0.4 0.12</ambient><diffuse>0.16 0.55 0.16</diffuse></material>
    </visual>''',
            f'''    <visual name="cb{uid}" cast_shadows="true">
      <pose>{x+r*0.5:.2f} {y+r*0.3:.2f} {z+h+r*0.95:.2f} 0 0 0</pose>
      <geometry><sphere><radius>{r*0.62:.2f}</radius></sphere></geometry>
      <material><ambient>0.12 0.4 0.12</ambient><diffuse>0.18 0.6 0.18</diffuse></material>
    </visual>''']


def bush_links(x, y, z, rng, uid=""):
    r = 0.9 + rng.uniform(0, 0.5)
    return [f'''    <visual name="b{uid}" cast_shadows="true">
      <pose>{x:.2f} {y:.2f} {z+r*0.6:.2f} 0 0 {rng.uniform(0, 6.28)}</pose>
      <geometry><sphere><radius>{r:.2f}</radius></sphere></geometry>
      <material><ambient>0.1 0.32 0.09</ambient><diffuse>0.2 0.55 0.15</diffuse></material>
    </visual>''']


def write_vegetation(water, holes, gray, m_per_px, terrain_w, terrain_d):
    """沿湖岸绿化带和岛上撒树(纯基元模型, 无外部依赖), 生成静态模型"""
    H, W = water.shape
    rng = np.random.default_rng(7)
    near_land = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8))
        .filter(ImageFilter.MaxFilter(81))) > 127      # 离水线 ~10m 内
    band = near_land & ~water                          # 岸边陆地带(含堤岸环)
    islands = holes.copy()
    parts = []
    n = 0
    step = 22                                          # ~5.3 m 网格
    for py in range(10, H - 10, step):
        for px in range(10, W - 10, step):
            place = None
            if band[py, px] and rng.random() < 0.55:
                place = (px, py, 'shore')
            elif islands[py, px] and rng.random() < 0.5:
                place = (px, py, 'island')
            if not place:
                continue
            px, py, where = place
            x = (px - W / 2) * m_per_px + rng.uniform(-1.5, 1.5)
            y = (H / 2 - py) * m_per_px + rng.uniform(-1.5, 1.5)
            # 地面高程 = 灰度映射 (bed_h .. land_h), 树基再下沉 0.15 防悬空
            z = (gray[py, px] / 255.0) * (CONFIG['land_h'] - CONFIG['bed_h']) \
                + CONFIG['bed_h'] - 0.15
            roll = rng.random()
            n += 1
            if where == 'island' or roll < 0.55:
                parts += round_tree_links(x, y, z, rng, uid=n)
            elif roll < 0.8:
                parts += palm_links(x, y, z, rng, uid=n)
            else:
                parts += bush_links(x, y, z, rng, uid=n)
    # 外圈林带: 沿地形边界外 18 m 的矩形环(参考 sydney 公园围合林带)
    ex, ey = terrain_w / 2 + 18, terrain_d / 2 + 18
    ring_pts = []
    for yy in np.arange(-ey, ey, 11):
        ring_pts += [(-ex, yy), (ex, yy)]
    for xx in np.arange(-ex, ex, 11):
        ring_pts += [(xx, -ey), (xx, ey)]
    for (x, y) in ring_pts:
        if rng.random() < 0.8:
            n += 1
            parts += round_tree_links(x, y, 1.45, rng, uid=n)

    model_dir = PKG / 'models' / 'dongpo_shore_trees'
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / 'model.config').write_text(
        '<?xml version="1.0"?>\n<model>\n  <name>dongpo_shore_trees</name>\n'
        '  <version>1.0</version>\n  <sdf version="1.9">model.sdf</sdf>\n'
        '  <description>shore vegetation generated by '
        'make_dongpo_terrain.py</description>\n</model>\n')
    sdf = ('<?xml version="1.0"?>\n<sdf version="1.9">\n  <model name='
           '"dongpo_shore_trees">\n    <static>true</static>\n'
           '    <link name="trees">\n'
           + '\n'.join(parts) + '\n    </link>\n  </model>\n</sdf>\n')
    (model_dir / 'model.sdf').write_text(sdf)
    print(f'植被: {n} 棵(含外圈林带) -> {model_dir}')


def write_apron(terrain_w, terrain_d, half=760.0):
    """地形外围大草坪环带(4 块静态盒子): 遮住地形边界外的深色海洋,
    让世界看起来是延伸到远处的公园陆地(sydney 风格)"""
    tx, ty = terrain_w / 2, terrain_d / 2
    top = 1.6
    boxes = [
        (-(tx + (half - tx) / 2), 0.0, (half - tx), half * 2),
        ((tx + (half - tx) / 2), 0.0, (half - tx), half * 2),
        (0.0, -(ty + (half - ty) / 2), tx * 2, (half - ty)),
        (0.0, (ty + (half - ty) / 2), tx * 2, (half - ty)),
    ]
    parts = []
    for i, (x, y, sx, sy) in enumerate(boxes):
        parts.append(
            '    <link name="apron' + str(i) + '">\n'
            '      <visual name="v">\n'
            f'        <pose>{x:.1f} {y:.1f} {top - 1.0:.1f} 0 0 0</pose>\n'
            f'        <geometry><box><size>{sx:.1f} {sy:.1f} 2.0</size>'
            '</box></geometry>\n'
            '        <material>\n'
            '          <ambient>0.18 0.36 0.13</ambient>\n'
            '          <diffuse>0.24 0.46 0.17</diffuse>\n'
            '        </material>\n'
            '      </visual>\n'
            '    </link>')
    model_dir = PKG / 'models' / 'dongpo_land_apron'
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / 'model.config').write_text(
        '<?xml version="1.0"?>\n<model>\n  <name>dongpo_land_apron</name>\n'
        '  <version>1.0</version>\n  <sdf version="1.9">model.sdf</sdf>\n'
        '  <description>grass apron around the satellite terrain'
        '</description>\n</model>\n')
    (model_dir / 'model.sdf').write_text(
        '<?xml version="1.0"?>\n<sdf version="1.9">\n  <model name='
        '"dongpo_land_apron">\n    <static>true</static>\n'
        + '\n'.join(parts) + '\n  </model>\n</sdf>\n')
    print(f'草坪环带: 4 块, 覆盖 ±{half:.0f} m -> {model_dir}')


def moments(mask):
    ys, xs = np.nonzero(mask)
    pts = np.stack([xs - xs.mean(), ys - ys.mean()]).astype(np.float64)
    cov = np.cov(pts)
    evals, evecs = np.linalg.eigh(cov)
    major = evecs[:, np.argmax(evals)]
    minor = evecs[:, np.argmin(evals)]
    proj = major[0] * pts[0] + major[1] * pts[1]
    proj2 = minor[0] * pts[0] + minor[1] * pts[1]
    return dict(cx=float(xs.mean()), cy=float(ys.mean()),
                theta=float(math.atan2(major[1], major[0])),
                major_px=float(proj.max() - proj.min()),
                minor_px=float(proj2.max() - proj2.min()),
                area=float(mask.sum()))


def main():
    sat = Image.open(CONFIG['sat_img']).convert('RGB')
    SW, SH = sat.size
    a = np.asarray(sat, dtype=np.int16)
    print(f'源图: {CONFIG["sat_img"]}  {SW}x{SH}')

    # ================= 1. 水体分割 =================
    r, b = a[..., 0], a[..., 2]
    v = np.asarray(sat.convert('HSV'), dtype=np.int16)[..., 2]
    water = ((b - r) > CONFIG['water_br_min']) & (b > CONFIG['water_b_min']) \
        & (v < CONFIG['water_v_max'])
    wm = Image.fromarray((water * 255).astype(np.uint8))
    wm = wm.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    wm = wm.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.MinFilter(7))
    water = np.asarray(wm) > 127
    water = largest_component(water)       # 去掉图幅内其他池塘
    print(f'湖体: {water.mean()*100:.1f}%  (分割总水体 '
          f'{(np.asarray(wm)>127).mean()*100:.1f}%)')

    # ================= 2. 湖中岛 =================
    holes = fill_enclosed_holes(water)
    for comp in components(holes):
        if len(comp) < CONFIG['min_island_px']:      # 小洞当噪声填成水
            ys, xs = zip(*comp)
            water[list(ys), list(xs)] = True
    holes = fill_enclosed_holes(water)               # 重算保留的岛
    n_island = len([c for c in components(holes) if len(c) >= CONFIG['min_island_px']])
    print(f'湖中岛: {n_island} 个, 共 {holes.sum()} px')

    # ================= 2b. 岸带补采(近岸浅/浑水补进水体) =================
    # 主分割用严阈值(B-R>35), 近岸浑水偏绿会被漏掉, 导致物理岸线比贴图
    # 水线缩进去几米。在岸界 ±band 内放宽阈值补采, 让物理岸线贴住贴图水线。
    band = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8))
        .filter(ImageFilter.MaxFilter(2 * CONFIG['shore_band_px'] + 1))) > 127
    inner = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8))
        .filter(ImageFilter.MinFilter(2 * CONFIG['shore_band_px'] + 1))) > 127
    shore_zone = band & ~inner
    # 平滑度条件: 近岸浅水平滑, 建筑/蓝色屋顶有纹理(实测 std 水面~2 vs 建筑~23)
    smooth = rolling_std(np.asarray(sat, np.float32)) \
        < CONFIG['shore_std_max']
    extra = shore_zone & ((b - r) > CONFIG['shore_br_min']) & (b > 70) \
        & (v < 165) & smooth
    added = int((extra & ~water).sum())
    water = water | extra
    water = largest_component(water)
    print(f'岸带补采: +{added} px')

    # ================= 3. 堤岸环带(防止船开出图幅) =================
    d = CONFIG['dike_px']
    dike = np.zeros_like(water)
    dike[d:SH-d, d:SW-d] = True
    water &= dike
    water = largest_component(water)
    # 贴图: 堤岸环带 + 沿水线绿化带涂成草坪绿(VRX 公园风格, 避免发黑)
    tex_arr = np.asarray(sat, np.float32).copy()
    rng2 = np.random.default_rng(42)
    noise = rng2.normal(0, 5, tex_arr.shape)
    grass = np.array([96, 138, 66])            # 草坪绿
    bank = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8))
        .filter(ImageFilter.MaxFilter(33))) > 127
    bank &= ~water                             # 水线 8m 内的岸带
    tex_arr[bank] = np.clip(grass + noise[bank], 0, 255)
    ring = ~dike
    tex_arr[ring] = np.clip(grass + noise[ring], 0, 255)

    # ================= 4. 比例标定 =================
    mm = moments(water)
    m_per_px = CONFIG['lake_length_m'] / mm['major_px']
    width_m = mm['minor_px'] * m_per_px
    print(f'湖长 {mm["major_px"]:.0f}px -> {m_per_px:.4f} m/px')
    print(f'湖宽校验: {width_m:.1f} m (实测 {CONFIG["lake_width_m"]} m, 偏差 '
          f'{abs(width_m-CONFIG["lake_width_m"])/CONFIG["lake_width_m"]*100:.0f}%)')
    terrain_w, terrain_d = SW * m_per_px, SH * m_per_px
    print(f'地形范围: {terrain_w:.1f} x {terrain_d:.1f} m')

    # ================= 5. 出生点(离岸最远的开阔水域) =================
    m = CONFIG['spawn_margin_px']
    ow = largest_component(np.asarray(
        Image.fromarray((water * 255).astype(np.uint8))
        .filter(ImageFilter.MinFilter(2 * m + 1))) > 127)
    if not ow.any():
        ow = water
    cy, cx = np.nonzero(ow)
    cx, cy = float(cx.mean()), float(cy.mean())
    spawn_x = (cx - SW / 2) * m_per_px
    spawn_y = (SH / 2 - cy) * m_per_px
    print(f'出生点: px=({cx:.0f},{cy:.0f})  world=({spawn_x:.1f},{spawn_y:.1f}) m')

    # ================= 6. 高度图 + 网格 + 贴图 =================
    span = CONFIG['land_h'] - CONFIG['bed_h']
    blur = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(CONFIG['shore_blur'])),
        dtype=np.float32) / 255.0
    # 灰度: 陆地亮(->+land_h), 水体暗(->bed_h); blur=1 是水 -> 取反
    gray = ((1.0 - blur) * 255).astype(np.uint8)

    # 湖中岛重算(供植被撒点)
    holes = fill_enclosed_holes(water)
    for comp in components(holes):
        if len(comp) < CONFIG['min_island_px']:
            ys, xs = zip(*comp)
            water[list(ys), list(xs)] = True
    holes = fill_enclosed_holes(water)

    TEX_DIR.mkdir(parents=True, exist_ok=True)
    hm_path = TEX_DIR / 'dongpo_heightmap.png'
    Image.fromarray(gray, 'L').resize(
        (CONFIG['out_res'],) * 2, Image.BILINEAR).save(hm_path)
    tex_path = TEX_DIR / 'dongpo_satellite.jpg'
    Image.fromarray(tex_arr.astype(np.uint8)).resize(
        (CONFIG['tex_res'],) * 2, Image.LANCZOS).save(tex_path, quality=90)
    print(f'高度图: {hm_path}  (灰度0 -> {CONFIG["bed_h"]}m, 255 -> '
          f'{CONFIG["land_h"]}m)')
    print(f'贴图:   {tex_path}')

    # ---- 地形网格(OBJ): 视觉用, 顶点高程=高度图真值, UV=贴图 ----
    step = CONFIG['mesh_step_px']
    nj, ni = gray.shape[0] // step, gray.shape[1] // step
    g = gray[::step, ::step] * (span / 255.0) + CONFIG['bed_h']
    mesh_dir = PKG / 'models' / 'dongpo_lake_terrain' / 'meshes'
    mesh_dir.mkdir(parents=True, exist_ok=True)
    obj_path = mesh_dir / 'dongpo_terrain.obj'
    with open(obj_path, 'w') as f:
        f.write('# dongpo lake terrain (meters). x=east, y=north, z up\n')
        for j in range(nj):
            for i in range(ni):
                x = (i / (ni - 1) - 0.5) * terrain_w
                y = (0.5 - j / (nj - 1)) * terrain_d
                f.write(f'v {x:.2f} {y:.2f} {float(g[j, i]):.2f}\n')
        for j in range(nj):                    # v=1 对应贴图顶行=北
            for i in range(ni):
                f.write(f'vt {i/(ni-1):.5f} {1-j/(nj-1):.5f}\n')
        gx, gy = np.gradient(g, terrain_d / (nj - 1), terrain_w / (ni - 1))
        for j in range(nj):
            for i in range(ni):
                nx, ny, nz = -gy[j, i], -gx[j, i], 1.0
                n = math.hypot(nx, ny, nz)
                f.write(f'vn {nx/n:.5f} {ny/n:.5f} {nz/n:.5f}\n')
        idx = lambda i, j: j * ni + i + 1
        for j in range(nj - 1):
            for i in range(ni - 1):
                aa, bb = idx(i, j), idx(i + 1, j)
                cc, dd = idx(i, j + 1), idx(i + 1, j + 1)
                f.write(f'f {aa}/{aa}/{aa} {cc}/{cc}/{cc} {bb}/{bb}/{bb}\n')
                f.write(f'f {bb}/{bb}/{bb} {cc}/{cc}/{cc} {dd}/{dd}/{dd}\n')
    print(f'网格:   {obj_path}  ({ni}x{nj} 顶点)')

    # ---- 岸边/岛上植被(纯基元静态模型) ----
    write_vegetation(water, holes, gray, m_per_px, terrain_w, terrain_d)
    write_apron(terrain_w, terrain_d)

    # ================= 7. 调试图 =================
    dbg = sat.copy()
    ov = np.asarray(dbg, np.float32)
    ov[water] = ov[water] * 0.55 + np.array([230, 40, 40]) * 0.45
    ov[holes] = ov[holes] * 0.5 + np.array([40, 230, 40]) * 0.5
    Image.fromarray(ov.astype(np.uint8)).save(TOOLS / 'debug_mask.png')
    print(f'调试图: {TOOLS / "debug_mask.png"} (水=红, 岛=绿)')

    meta = dict(m_per_px=m_per_px, terrain_w=terrain_w, terrain_d=terrain_d,
                land_h=CONFIG['land_h'], bed_h=CONFIG['bed_h'],
                spawn_x=spawn_x, spawn_y=spawn_y,
                lakes=(SW - 2 * d) * m_per_px)
    (TOOLS / 'dongpo_terrain_meta.json').write_text(
        json.dumps(meta, indent=2, ensure_ascii=False))
    print(f'元数据: {TOOLS / "dongpo_terrain_meta.json"}')


if __name__ == '__main__':
    main()
