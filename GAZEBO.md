# Moving from the built-in simulator to Gazebo (still ₹0)

The brain never touches the simulator directly. It only sends assignment
messages and reads delivered messages through `tower/sim/comms.py`. So moving to
Gazebo means replacing the *physics* in `tower/sim/world.py`. The brain,
the robot runtime logic and the console stay the same.

**Status:** not built yet. This is the plan.

## Free stack

- Windows: install WSL2 with Ubuntu 24.04 (`wsl --install -d Ubuntu-24.04`)
- ROS 2 Jazzy + Gazebo Harmonic (`ros-jazzy-desktop`, `ros-jazzy-ros-gz`)
- PX4 v1.17 SITL for the one "real flight code" drone (SCOUT-2)

## What to replace

| `SimWorld` method (2D) | Gazebo equivalent |
|---|---|
| `fly_to(body, target, speed, dt)` | publish a velocity toward target on `/model/<id>/cmd_vel` (MulticopterVelocityControl), or a PX4 offboard setpoint over uXRCE-DDS |
| `drive_to(body, waypoint, speed, dt)` | publish `geometry_msgs/Twist` on `/<id>/cmd_vel` (DiffDrive) |
| `body.pos`, `body.vel` | subscribe to `/model/<id>/odometry` (OdometryPublisher) |
| `body.battery` | subscribe to `/model/<id>/battery/main/state` (LinearBatteryPlugin) |
| `sense(body, dt)` | LogicalCamera topic → keep the same noisy-detector logic, filtered on `victim_*` model names |
| `add_debris`, `kill`, `add_survivor` | `gz service -s /world/landslide/create` / `remove` (UserCommands) |
| `set_wind` | `gz topic -t /world/landslide/wind` (WindEffects) |
| `abs_alt(body)` | odometry z |

Keep `comms.py` in the loop between the robots and the brain, so TOWER still
only knows what a real radio would have delivered.

## Suggested order

1. Generate `world.sdf` from `scenarios/landslide_v1.json` (sectors, roads, ridge heightmap, river strip, victims).
2. Write `tower/sim/gz_world.py` with the same methods as `SimWorld`, backed by `rclpy` publishers and subscribers.
3. Run one rover, then one drone, against the unchanged `Robot` runtime (`--world gz` flag in `runtime.py`).
4. Add SCOUT-2 through PX4 SITL last.
