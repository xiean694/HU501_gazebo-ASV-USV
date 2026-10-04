# 东坡湖（dongpo_lake）双船启动：hu501 + wamv 停在湖心开阔水域
# 用法: ros2 launch hu501urdf hu501.launch.py
#       ros2 launch hu501urdf hu501.launch.py headless:=true
# 出生点来自 tools/make_dongpo_terrain.py 输出（lake 开阔水域质心），
# 重新生成地形后如湖心变化，同步修改下方 HU501_XYZ_RPY / WAMV_XYZ_RPY。
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.actions import OpaqueFunction
from launch.substitutions import LaunchConfiguration

from ament_index_python.packages import get_package_share_directory

import vrx_gz.launch
from vrx_gz.model import Model

# 出生点：北岸近岸泊位（离北岸约 25 m，船头朝湖心）
HU501_XYZ_RPY = [-12.0, 100.0, 0, 0, 0, 3.14]
WAMV_XYZ_RPY = [-6.0, 100.0, 0, 0, 0, 3.14]


def launch(context, *args, **kwargs):
    world_name = LaunchConfiguration('world').perform(context)
    headless = LaunchConfiguration('headless').perform(context).lower() == 'true'

    world_name, _ = os.path.splitext(world_name)
    world_name_base = os.path.basename(world_name)

    hu501_urdf = os.path.join(
        get_package_share_directory('hu501urdf'),
        'urdf', 'hu501urdf.urdf.xacro')

    models = [
        Model('wamv', 'wam-v', WAMV_XYZ_RPY),
        Model('hu501', 'wam-v', HU501_XYZ_RPY),
    ]
    models[1].set_urdf(hu501_urdf)

    launch_processes = []
    launch_processes.extend(vrx_gz.launch.simulation(
        world_name, headless, False, ''))
    launch_processes.extend(vrx_gz.launch.spawn(
        'full', world_name_base, models, None))
    launch_processes.extend(vrx_gz.launch.competition_bridges(
        world_name_base, False))
    return launch_processes


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('world', default_value='dongpo_lake'),
        DeclareLaunchArgument('headless', default_value='False'),
        OpaqueFunction(function=launch),
    ])
