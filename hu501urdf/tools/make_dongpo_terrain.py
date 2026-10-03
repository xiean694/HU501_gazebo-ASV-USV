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
    tex_res=4096,                       # 卫星贴图边长
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
    # ---- 建筑(卫星图屋顶检测) ----
    roof_v_min=115,                     # 白/灰屋顶: 亮度下限
    roof_sat_max=45,                    # 白/灰屋顶: 色度(max-min)上限
    roof_red_rb=22,                     # 红褐屋顶: r-b 下限
    bld_min_px=500,                     # 建筑连通域面积范围(像素)
    bld_max_px=45000,
    bld_fill_min=0.50,                  # bbox 填充率(排掉树影/碎块)
    bld_aspect_max=4.2,                 # 长宽比上限(排掉道路)
    # ---- 红树林湿地 ----
    island_top_h=0.8,                   # 红树林岛顶高程(低于陆地, 滩涂感)
    mangrove_step_px=8,                 # 红树丛间距(像素 ~1.9 m)
    egret_n=14,                         # 白鹭数量(白鹭栖息地)
    # ---- 湖中堤/栈道(亮色窄条) ----
    jetty_v_min=135,                    # 亮度下限
    jetty_min_px=110,                   # 最小面积
    jetty_w_max_px=14,                  # 最大宽度(像素 ~3.4 m)
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
    """椰子树(海南校园行道树): 高弯干 + 放射羽状叶 + 椰果
    航拍实测: 树高 8~12 m, 树干向湖面方向倾斜"""
    h = 9.8 + rng.uniform(-1.8, 2.2)
    lean = rng.uniform(-0.28, 0.28)
    parts = [f'''    <visual name="t{uid}" cast_shadows="true">
      <pose>{x} {y} {z+h/2} 0 {lean} {rng.uniform(0, 6.28)}</pose>
      <geometry><cylinder><radius>0.12</radius><length>{h:.2f}</length></cylinder></geometry>
      <material><ambient>0.34 0.26 0.16</ambient><diffuse>0.44 0.34 0.21</diffuse></material>
    </visual>''']
    tx = x - math.sin(lean) * h * 0.5
    ty = y
    cz = z + h - 0.08
    for k in range(9):
        yaw = k * 6.2832 / 9 + rng.uniform(-0.18, 0.18)
        parts.append(f'''    <visual name="f{uid}_{k}" cast_shadows="true">
      <pose>{tx:.2f} {ty:.2f} {cz:.2f} 0 {-0.38 + rng.uniform(-0.12, 0.12):.2f} {yaw:.2f}</pose>
      <geometry><box><size>0.42 3.2 0.04</size></box></geometry>
      <material><ambient>0.07 0.32 0.09</ambient><diffuse>0.13 0.5 0.13</diffuse></material>
    </visual>''')
    for k in range(3):
        yaw = rng.uniform(0, 6.28)
        rad = 0.32
        parts.append(f'''    <visual name="c{uid}_{k}" cast_shadows="true">
      <pose>{tx + math.cos(yaw) * rad:.2f} {ty + math.sin(yaw) * rad:.2f} {cz - 0.18:.2f} 0 0 0</pose>
      <geometry><sphere><radius>0.09</radius></sphere></geometry>
      <material><ambient>0.32 0.24 0.1</ambient><diffuse>0.45 0.34 0.12</diffuse></material>
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


def write_vegetation(water, holes, gray, m_per_px, terrain_w, terrain_d, blds):
    """沿湖岸绿化带和岛上撒树(纯基元模型, 无外部依赖), 生成静态模型"""
    H, W = water.shape
    rng = np.random.default_rng(7)
    near_land = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8))
        .filter(ImageFilter.MaxFilter(81))) > 127      # 离水线 ~10m 内
    band = near_land & ~water                          # 岸边陆地带(含堤岸环)
    bmask = np.zeros_like(water)                       # 建筑占地不种树
    for (x0, y0, sx, sy, rgb) in blds:
        bmask[max(0, y0 - 4):y0 + sy + 4, max(0, x0 - 4):x0 + sx + 4] = True
    band &= ~bmask
    parts = []
    n = 0
    step = 22                                          # ~5.3 m 网格
    for py in range(10, H - 10, step):
        for px in range(10, W - 10, step):
            if not (band[py, px] and rng.random() < 0.55):
                continue
            x = (px - W / 2) * m_per_px + rng.uniform(-1.5, 1.5)
            y = (H / 2 - py) * m_per_px + rng.uniform(-1.5, 1.5)
            # 地面高程 = 灰度映射 (bed_h .. land_h), 树基再下沉 0.15 防悬空
            z = (gray[py, px] / 255.0) * (CONFIG['land_h'] - CONFIG['bed_h']) \
                + CONFIG['bed_h'] - 0.15
            roll = rng.random()
            n += 1
            if roll < 0.62:
                parts += palm_links(x, y, z, rng, uid=n)
            elif roll < 0.85:
                parts += round_tree_links(x, y, z, rng, uid=n)
            else:
                parts += bush_links(x, y, z, rng, uid=n)
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
    print(f'植被: {n} 棵 -> {model_dir}')


def dilate_mask(mask, px):
    return np.asarray(
        Image.fromarray((mask * 255).astype(np.uint8))
        .filter(ImageFilter.MaxFilter(2 * px + 1))) > 127


def erode_mask(mask, px):
    return np.asarray(
        Image.fromarray((mask * 255).astype(np.uint8))
        .filter(ImageFilter.MinFilter(2 * px + 1))) > 127


def detect_buildings(sat, water, dike):
    """卫星图屋顶检测 -> [(x0,y0,sx,sy,rgb), ...]
    判据: 陆地上(离水线 12px 外, 图幅内)的低饱和亮色(白/灰平顶)
    或红褐色屋顶; 排除绿色植被。
    成排屋顶会经亮色地面连成大片, 先腐蚀 3px 打断窄连接再分连通域,
    bbox 面积/填充率统计回到未腐蚀的候选掩膜。"""
    a = np.asarray(sat, np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    v = a.max(-1)
    chroma = v - a.min(-1)
    bright_roof = (v > CONFIG['roof_v_min']) & (chroma < CONFIG['roof_sat_max'])
    red_roof = (r - b > CONFIG['roof_red_rb']) & (r > 115) & (v > 100)
    green = g - np.maximum(r, b) > 10
    land = dike & ~dilate_mask(water, 12)
    cand = (bright_roof | red_roof) & ~green & land
    # 南侧民房区屋顶更暗更小, 单独放宽阈值与面积下限
    H, W = a.shape[:2]
    south = np.zeros_like(cand)
    south[int(H * 0.66):, :] = True
    south_roof = (v > 100) & (chroma < 50) & ~green & land & south
    cand = cand | south_roof
    seeds = erode_mask(cand, 3)                    # 断开屋顶间窄连接
    out = []
    for comp in components(seeds):
        if len(comp) < 150:
            continue
        ys, xs = zip(*comp)
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        sx, sy = x1 - x0 + 1, y1 - y0 + 1
        sub = cand[y0:y1 + 1, x0:x1 + 1]           # bbox 内统计回原图
        n = int(sub.sum())
        fill = n / (sx * sy)
        aspect = max(sx / sy, sy / sx)
        min_px = 300 if y0 > H * 0.66 else CONFIG['bld_min_px']
        if not (min_px <= n <= CONFIG['bld_max_px']):
            continue
        if fill < CONFIG['bld_fill_min'] or aspect > CONFIG['bld_aspect_max']:
            continue
        sel = np.asarray(sat, np.int16)[y0:y1 + 1, x0:x1 + 1][sub]
        rgb = sel.mean(0)
        out.append((x0, y0, sx, sy, rgb))
    return out


def building_height(nx, ny, rng):
    """按卫星图区位定楼高: 西/北侧校园教学楼高, 南侧民房矮"""
    if ny > 0.66:
        base = 7.0                     # 南侧低矮民房 2-3 层
    elif nx > 0.72 and ny < 0.45:
        base = 13.0                    # 东北砖红顶建筑群
    elif nx < 0.45:
        base = 16.0                    # 西侧教学楼/宿舍 4-6 层
    else:
        base = 12.0
    return base * rng.uniform(0.75, 1.35)


def write_buildings(blds, gray, m_per_px, W, H):
    """3D 建筑(平顶盒子 + 女儿墙色顶板 + 屋顶设备间), 颜色采样自卫星图"""
    rng = np.random.default_rng(11)
    parts = []
    # 航拍实测: 校园楼群米黄/暖白外立面, 屋面陈旧偏灰
    wall = ('<material><ambient>0.78 0.72 0.58</ambient>'
            '<diffuse>0.84 0.78 0.64</diffuse></material>')
    for i, (x0, y0, sx, sy, rgb) in enumerate(blds):
        cx, cy = x0 + sx / 2, y0 + sy / 2
        wx = (cx - W / 2) * m_per_px
        wy = (H / 2 - cy) * m_per_px
        zb = (gray[int(min(max(cy, 0), H - 1)), int(min(max(cx, 0), W - 1))]
              / 255.0) * (CONFIG['land_h'] - CONFIG['bed_h']) \
            + CONFIG['bed_h'] - 0.4
        h = building_height(cx / W, cy / H, rng)
        bx = max(sx * m_per_px, 6.0)
        by = max(sy * m_per_px, 6.0)
        parts.append(f'''    <visual name="bld{i}" cast_shadows="true">
      <pose>{wx:.2f} {wy:.2f} {zb + h / 2:.2f} 0 0 {rng.uniform(-0.02, 0.02):.2f}</pose>
      <geometry><box><size>{bx:.2f} {by:.2f} {h:.2f}</size></box></geometry>
      {wall}
    </visual>''')
        # 窗带(航拍: 横向排窗): 每层一条深色横带贴四个立面
        n_f = max(2, int((h - 1.2) // 3.2))
        win = ('<material><ambient>0.32 0.37 0.42</ambient>'
               '<diffuse>0.38 0.43 0.48</diffuse></material>')
        for k in range(n_f):
            wz = zb + 2.2 + k * 3.2
            parts.append(f'''    <visual name="wx{i}_{k}" cast_shadows="false">
      <pose>{wx:.2f} {wy + by / 2 + 0.03:.2f} {wz:.2f} 0 0 0</pose>
      <geometry><box><size>{bx * 0.88:.2f} 0.06 1.3</size></box></geometry>
      {win}
    </visual>''')
            parts.append(f'''    <visual name="ws{i}_{k}" cast_shadows="false">
      <pose>{wx:.2f} {wy - by / 2 - 0.03:.2f} {wz:.2f} 0 0 0</pose>
      <geometry><box><size>{bx * 0.88:.2f} 0.06 1.3</size></box></geometry>
      {win}
    </visual>''')
            parts.append(f'''    <visual name="we{i}_{k}" cast_shadows="false">
      <pose>{wx + bx / 2 + 0.03:.2f} {wy:.2f} {wz:.2f} 0 0 0</pose>
      <geometry><box><size>0.06 {by * 0.88:.2f} 1.3</size></box></geometry>
      {win}
    </visual>''')
            parts.append(f'''    <visual name="wn{i}_{k}" cast_shadows="false">
      <pose>{wx - bx / 2 - 0.03:.2f} {wy:.2f} {wz:.2f} 0 0 0</pose>
      <geometry><box><size>0.06 {by * 0.88:.2f} 1.3</size></box></geometry>
      {win}
    </visual>''')
        col = np.clip(np.array(rgb) / 255.0 * 0.85, 0, 1)   # 屋面陈旧压暗
        parts.append(f'''    <visual name="roof{i}" cast_shadows="true">
      <pose>{wx:.2f} {wy:.2f} {zb + h + 0.12:.2f} 0 0 0</pose>
      <geometry><box><size>{bx + 0.6:.2f} {by + 0.6:.2f} 0.35</size></box></geometry>
      <material><ambient>{col[0]:.2f} {col[1]:.2f} {col[2]:.2f}</ambient><diffuse>{col[0]:.2f} {col[1]:.2f} {col[2]:.2f}</diffuse></material>
    </visual>''')
        if rng.random() < 0.6:                       # 屋顶设备间/楼梯间
            parts.append(f'''    <visual name="eq{i}" cast_shadows="true">
      <pose>{wx + rng.uniform(-0.2, 0.2) * bx:.2f} {wy + rng.uniform(-0.2, 0.2) * by:.2f} {zb + h + 0.75:.2f} 0 0 0</pose>
      <geometry><box><size>{bx * 0.3:.2f} {by * 0.3:.2f} 1.1</size></box></geometry>
      <material><ambient>0.6 0.6 0.6</ambient><diffuse>0.66 0.66 0.66</diffuse></material>
    </visual>''')
    model_dir = PKG / 'models' / 'dongpo_buildings'
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / 'model.config').write_text(
        '<?xml version="1.0"?>\n<model>\n  <name>dongpo_buildings</name>\n'
        '  <version>1.0</version>\n  <sdf version="1.9">model.sdf</sdf>\n'
        '  <description>campus buildings detected from satellite image'
        '</description>\n</model>\n')
    sdf = ('<?xml version="1.0"?>\n<sdf version="1.9">\n  <model name='
           '"dongpo_buildings">\n    <static>true</static>\n'
           '    <link name="buildings">\n'
           + '\n'.join(parts) + '\n    </link>\n  </model>\n</sdf>\n')
    (model_dir / 'model.sdf').write_text(sdf)
    print(f'建筑: {len(blds)} 栋 -> {model_dir}')


def write_mangrove(holes, gray, m_per_px, W, H):
    """湖中红树林湿地(东坡湖 2006 年起引种红海榄/海桑)+ 白鹭
    密集矮绿丛, 每 ~1.9 m 一簇, 岛顶高程由 island_top_h 控制"""
    rng = np.random.default_rng(23)
    parts = []
    n = 0
    step = CONFIG['mangrove_step_px']
    for py in range(6, H - 6, step):
        for px in range(6, W - 6, step):
            if not holes[py, px] or rng.random() > 0.9:
                continue
            x = (px - W / 2) * m_per_px + rng.uniform(-0.8, 0.8)
            y = (H / 2 - py) * m_per_px + rng.uniform(-0.8, 0.8)
            zb = (gray[py, px] / 255.0) * (CONFIG['land_h'] - CONFIG['bed_h']) \
                + CONFIG['bed_h']
            n += 1
            for k in range(rng.integers(2, 4)):       # 每簇 2-3 个冠球
                r = 0.8 + rng.uniform(0, 0.8)
                dx, dy = rng.uniform(-0.9, 0.9), rng.uniform(-0.9, 0.9)
                tone = rng.uniform(0.8, 1.25)
                parts.append(f'''    <visual name="m{n}_{k}" cast_shadows="true">
      <pose>{x + dx:.2f} {y + dy:.2f} {zb + r * 0.75:.2f} 0 0 0</pose>
      <geometry><sphere><radius>{r:.2f}</radius></sphere></geometry>
      <material><ambient>{0.06 * tone:.3f} {0.26 * tone:.3f} {0.08 * tone:.3f}</ambient><diffuse>{0.1 * tone:.3f} {0.42 * tone:.3f} {0.11 * tone:.3f}</diffuse></material>
    </visual>''')
    # 白鹭(湖心岛是白鹭栖息地): 细腿 + 白身 + 头
    ys, xs = np.nonzero(holes)
    for i in range(CONFIG['egret_n']):
        if len(ys) == 0:
            break
        j = rng.integers(0, len(ys))
        x = (xs[j] - W / 2) * m_per_px
        y = (H / 2 - ys[j]) * m_per_px
        zb = (gray[ys[j], xs[j]] / 255.0) * (CONFIG['land_h'] - CONFIG['bed_h']) \
            + CONFIG['bed_h']
        yaw = rng.uniform(0, 6.28)
        parts.append(f'''    <visual name="eg{i}" cast_shadows="true">
      <pose>{x:.2f} {y:.2f} {zb + 0.22:.2f} 0 0 {yaw:.2f}</pose>
      <geometry><cylinder><radius>0.025</radius><length>0.45</length></cylinder></geometry>
      <material><ambient>0.85 0.85 0.85</ambient><diffuse>0.9 0.9 0.9</diffuse></material>
    </visual>''')
        parts.append(f'''    <visual name="eb{i}" cast_shadows="true">
      <pose>{x:.2f} {y:.2f} {zb + 0.52:.2f} 0 {0.5:.2f} {yaw:.2f}</pose>
      <geometry><sphere><radius>0.11</radius></sphere></geometry>
      <material><ambient>0.9 0.9 0.88</ambient><diffuse>0.95 0.95 0.93</diffuse></material>
    </visual>''')
    model_dir = PKG / 'models' / 'dongpo_mangrove'
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / 'model.config').write_text(
        '<?xml version="1.0"?>\n<model>\n  <name>dongpo_mangrove</name>\n'
        '  <version>1.0</version>\n  <sdf version="1.9">model.sdf</sdf>\n'
        '  <description>mangrove wetland and egrets on lake islands'
        '</description>\n</model>\n')
    sdf = ('<?xml version="1.0"?>\n<sdf version="1.9">\n  <model name='
           '"dongpo_mangrove">\n    <static>true</static>\n'
           '    <link name="mangrove">\n'
           + '\n'.join(parts) + '\n    </link>\n  </model>\n</sdf>\n')
    (model_dir / 'model.sdf').write_text(sdf)
    print(f'红树: {n} 簇 + 白鹭 {CONFIG["egret_n"]} 只 -> {model_dir}')


def write_jetties(sat, water, blds, m_per_px, W, H):
    """亲水平台自动检测(贴水线 8px 内的亮色实心小结构) + 手动西南弧形
    观景堤(与岸相连, 自动检测不可靠), 均带 collision 可被船撞"""
    a = np.asarray(sat, np.int16)
    r_, g_, b_ = a[..., 0], a[..., 1], a[..., 2]
    v = a.max(-1)
    zone = dilate_mask(water, 8)
    green = g_ - np.maximum(r_, b_) > 10
    cand = zone & ~water & (v > CONFIG['jetty_v_min']) & ~green
    bmask = np.zeros_like(water)                 # 排除已检出的建筑
    for (x0, y0, sx, sy, rgb) in blds:
        bmask[max(0, y0 - 6):y0 + sy + 6, max(0, x0 - 6):x0 + sx + 6] = True
    cand &= ~bmask
    d = CONFIG['dike_px']
    ring = np.zeros_like(water)
    ring[d:H - d, d:W - d] = True
    cand &= ring
    rng = np.random.default_rng(31)
    parts = []
    out_segs = []
    n = 0

    def emit(wx, wy, yaw, seg, w_m):
        nonlocal n
        parts.append(f'''    <visual name="jv{n}" cast_shadows="true">
      <pose>{wx:.2f} {wy:.2f} 0.05 0 0 {yaw:.3f}</pose>
      <geometry><box><size>{seg:.2f} {w_m:.2f} 1.1</size></box></geometry>
      <material><ambient>0.68 0.65 0.6</ambient><diffuse>0.76 0.73 0.68</diffuse></material>
    </visual>''')
        parts.append(f'''    <collision name="jc{n}">
      <pose>{wx:.2f} {wy:.2f} 0.05 0 0 {yaw:.3f}</pose>
      <geometry><box><size>{seg:.2f} {w_m:.2f} 1.1</size></box></geometry>
    </collision>''')
        n += 1
        out_segs.append((wx, wy))

    for comp in components(cand):
        if len(comp) < CONFIG['jetty_min_px']:
            continue
        ys, xs = zip(*comp)
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        sx, sy = x1 - x0 + 1, y1 - y0 + 1
        long_px = max(sx, sy)
        w_px = len(comp) / long_px            # 平均宽度
        fill = len(comp) / (sx * sy)
        # fill 过滤掉岛缘环形滩涂, 只留实心小平台/短栈桥
        if w_px > CONFIG['jetty_w_max_px'] or w_px < 1.2 or long_px < 12 \
           or fill < 0.55:
            continue
        pts = np.stack([np.array(xs), np.array(ys)], -1).astype(np.float64)
        pts -= pts.mean(0)
        cov = np.cov(pts.T)
        evals, evecs = np.linalg.eigh(cov)
        major = evecs[:, np.argmax(evals)]    # 图像坐标主轴 (dx, dy_img)
        proj = pts @ major
        order = np.argsort(proj)
        step_px = 5
        w_m = min(w_px * m_per_px + 1.2, 3.4)
        pts_abs = pts + np.array([np.mean(xs), np.mean(ys)])
        for j in order[::step_px]:
            px, py = pts_abs[j]
            wx = (px - W / 2) * m_per_px
            wy = (H / 2 - py) * m_per_px
            yaw = math.atan2(-major[1], major[0])
            emit(wx, wy, yaw, step_px * m_per_px * 1.15, w_m)

    # ---- 西南弧形观景堤(卫星图原像素 (460,1060)->(360,940), 凸向湖内) ----
    P1 = np.array([-146.0, -101.0])
    P2 = np.array([-170.0, -72.0])
    Dv = P2 - P1
    Nv = np.array([Dv[1], -Dv[0]]) / np.linalg.norm(Dv)
    if Nv @ (-P1 - P2) < 0:                   # 保证法向指向湖心
        Nv = -Nv
    Mv = (P1 + P2) / 2 + Nv * 7.0             # 弧顶
    C = 2 * Mv - (P1 + P2) / 2                # 二次贝塞尔控制点
    ts = np.linspace(0, 1, 15)
    arc = np.array([(1 - t) ** 2 * P1 + 2 * (1 - t) * t * C + t ** 2 * P2
                    for t in ts])
    for k in range(len(arc) - 1):
        mid = (arc[k] + arc[k + 1]) / 2
        dv = arc[k + 1] - arc[k]
        emit(mid[0], mid[1], math.atan2(dv[1], dv[0]),
             np.linalg.norm(dv) + 0.35, 2.6)

    model_dir = PKG / 'models' / 'dongpo_jetties'
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / 'model.config').write_text(
        '<?xml version="1.0"?>\n<model>\n  <name>dongpo_jetties</name>\n'
        '  <version>1.0</version>\n  <sdf version="1.9">model.sdf</sdf>\n'
        '  <description>levees and boardwalks detected in the lake'
        '</description>\n</model>\n')
    sdf = ('<?xml version="1.0"?>\n<sdf version="1.9">\n  <model name='
           '"dongpo_jetties">\n    <static>true</static>\n'
           '    <link name="jetties">\n'
           + '\n'.join(parts) + '\n    </link>\n  </model>\n</sdf>\n')
    (model_dir / 'model.sdf').write_text(sdf)
    print(f'堤/栈道: {n} 段 -> {model_dir}')
    return out_segs


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
    # 贴图: 全部陆地草色化(用户反馈: 陆地发黑不像花草环境)。
    # 亮度调制保留地形纹理, 低频斑块模拟草丛, 彻底消除黑色地面。
    tex_arr = np.asarray(sat, np.float32).copy()
    rng2 = np.random.default_rng(42)
    noise = rng2.normal(0, 6, tex_arr.shape)
    grass = np.array([128, 140, 66])           # 草坪黄绿(航拍: 偏枯黄)
    bank = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8))
        .filter(ImageFilter.MaxFilter(33))) > 127
    bank &= ~water                             # 水线 8m 内的岸带
    tex_arr[bank] = np.clip(grass + noise[bank], 0, 255)
    ring = ~dike
    tex_arr[ring] = np.clip(grass + noise[ring], 0, 255)
    # 陆地: 草色 × 原亮度(保留等高线/道路细节), 加低频草丛斑块
    land = dike & ~water
    scale = np.clip(v / 150.0, 0.45, 1.05)[:, :, None]
    patch = np.asarray(Image.fromarray(
        (rng2.normal(0, 1, (SH // 12, SW // 12)) * 255).astype(np.uint8)
    ).convert('L').resize((SW, SH)), np.float32) / 255.0
    patch = 0.88 + patch * 0.24                # 斑块乘子 0.88~1.12
    base = grass[None, None, :] * scale * patch[:, :, None]
    tex_arr[land] = np.clip(base[land] + noise[land] * 1.6, 0, 255)

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
    # 红树林岛顶降至 island_top_h(滩涂湿地低于陆地, 取 min 保留岸边渐变)
    isl_gray = int((CONFIG['island_top_h'] - CONFIG['bed_h'])
                   / (CONFIG['land_h'] - CONFIG['bed_h']) * 255)
    gray[holes] = np.minimum(gray[holes], isl_gray)

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

    # ---- 建筑 / 植被 / 红树林 / 堤栈道 ----
    blds = detect_buildings(sat, water, dike)
    write_vegetation(water, holes, gray, m_per_px, terrain_w, terrain_d, blds)
    write_buildings(blds, gray, m_per_px, SW, SH)
    write_mangrove(holes, gray, m_per_px, SW, SH)
    segs = write_jetties(sat, water, blds, m_per_px, SW, SH)

    # ================= 7. 调试图 =================
    ov = np.asarray(sat, np.float32)
    ov[water] = ov[water] * 0.55 + np.array([230, 40, 40]) * 0.45
    ov[holes] = ov[holes] * 0.5 + np.array([40, 230, 40]) * 0.5
    dbg = Image.fromarray(ov.astype(np.uint8))
    draw = ImageDraw.Draw(dbg)
    for (x0, y0, sx, sy, rgb) in blds:
        draw.rectangle([x0, y0, x0 + sx, y0 + sy], outline=(60, 120, 250),
                       width=4)
    for (wx, wy) in segs:
        jx = int(wx / m_per_px + SW / 2)
        jy = int(SH / 2 - wy / m_per_px)
        draw.ellipse([jx - 4, jy - 4, jx + 4, jy + 4], fill=(255, 220, 0)
                     if 0 <= jx < SW and 0 <= jy < SH else None)
    dbg.save(TOOLS / 'debug_mask.png')
    print(f'调试图: {TOOLS / "debug_mask.png"} '
          '(水=红, 岛=绿, 建筑=蓝框, 堤段=黄点)')

    meta = dict(m_per_px=m_per_px, terrain_w=terrain_w, terrain_d=terrain_d,
                land_h=CONFIG['land_h'], bed_h=CONFIG['bed_h'],
                spawn_x=spawn_x, spawn_y=spawn_y,
                lakes=(SW - 2 * d) * m_per_px)
    (TOOLS / 'dongpo_terrain_meta.json').write_text(
        json.dumps(meta, indent=2, ensure_ascii=False))
    print(f'元数据: {TOOLS / "dongpo_terrain_meta.json"}')


if __name__ == '__main__':
    main()
