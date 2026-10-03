"""Built-in 2D simulator: the stand-in for Gazebo.

world.py   ground truth (positions, batteries, wind, debris, survivors)
robot.py   the software that runs on each robot (behavior tree, playbook, auction)
comms.py   radio emulator between robots and the base
injector.py disruptions (debris, failures, wind, blackouts)

TOWER's brain never reads world.py directly; it only sees what comms.py delivers.
To move to Gazebo, replace world.py's physics with ROS 2 topics (see GAZEBO.md).
"""
