# 双船启动：默认 WAM-V + hu501 并排停在 sydney_regatta 起点附近
# 用法: ros2 launch hu501urdf dual.launch.py
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.actions import OpaqueFunction
from launch.substitutions import LaunchConfiguration

from ament_index_python.packages import get_package_share_directory

import vrx_gz.launch
from vrx_gz.model import Model

# 出生点：wamv 为 VRX 默认起点 (-532, 162)，hu501 停在其 +x 方向 3 m 旁
WAMV_XYZ_RPY = [-532, 162, 0, 0, 0, 1]
HU501_XYZ_RPY = [-510, 172, 0, 0, 0, 1]


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
        DeclareLaunchArgument('world', default_value='sydney_regatta'),
        DeclareLaunchArgument('headless', default_value='False'),
        OpaqueFunction(function=launch),
    ])
