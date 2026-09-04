from setuptools import find_packages, setup

package_name = "sunnybotics_adapter"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="SunnyboticsOS Machine Layer",
    maintainer_email="maintainers@sunnybotics.invalid",
    description=(
        "Bridges ROS 2 machines onto the SunnyboticsOS REST/JSON contract: "
        "rclpy on a background thread, FastAPI on the main one."
    ),
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "adapter = sunnybotics_adapter.main:main",
        ],
    },
)
