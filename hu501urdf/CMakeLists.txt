cmake_minimum_required(VERSION 3.8)
project(hu501urdf)

find_package(ament_cmake REQUIRED)

install(DIRECTORY urdf meshes launch worlds models tools
  DESTINATION share/${PROJECT_NAME})

# 环境钩子：把 share 目录加入 GZ_SIM_RESOURCE_PATH，
# 使 model://hu501urdf/meshes/... 能被 gz 解析
ament_environment_hooks("hooks/resource_paths.dsv.in")

ament_package()