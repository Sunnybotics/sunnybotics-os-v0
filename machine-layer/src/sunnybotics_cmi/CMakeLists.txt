cmake_minimum_required(VERSION 3.8)
project(sunnybotics_cmi)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)

# MachineState.stamp is a builtin_interfaces/Time.
find_package(builtin_interfaces REQUIRED)
# Every action pulls in action_msgs (goal handles, GoalStatus, cancellation).
find_package(action_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/MachineState.msg"
  "action/Mission.action"
  DEPENDENCIES
    builtin_interfaces
    action_msgs
)

# Downstream packages get the runtime bits by depending on this one.
ament_export_dependencies(rosidl_default_runtime)

ament_package()
