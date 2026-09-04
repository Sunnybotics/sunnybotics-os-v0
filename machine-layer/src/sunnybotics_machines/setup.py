import os
from glob import glob

from setuptools import find_packages, setup

package_name = "sunnybotics_machines"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="SunnyboticsOS Machine Layer",
    maintainer_email="maintainers@sunnybotics.invalid",
    description=(
        "SIMULATED machines for SunnyboticsOS V0: one reusable node class "
        "speaking the Common Machine Interface, instantiated twice."
    ),
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "rover_01 = sunnybotics_machines.rover_01:main",
            "rover_02 = sunnybotics_machines.rover_02:main",
        ],
    },
)
