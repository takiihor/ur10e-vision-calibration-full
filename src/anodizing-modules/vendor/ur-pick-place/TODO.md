# Open work for review

- Done on this review branch: UR10e arm preview, separate editable tool/camera/mount boxes, cell boxes and saved joint-path review.
- Reviewed hand-eye tilt and roll controls, path checks for probes, views and returns, and the checks before each joint command. The default scene blocks automatic motion.
- Done offline: The editor labels tool0 and base box frames and identifies the generic robot model. Firefox reviewed the teacher and calibration editors with an inert service. Open: robot-specific geometry and uncertainty verification, measured cell coverage, tracking/path-fidelity validation and an attended hardware trial.
- Open in Marker Pick Teacher: integrate actual Cartesian stages and return-to-observation with a suitable path validator. Its new panel is visual only.
- Read [the implementation guide](docs/COLLISION_REVIEW.md).

- Verify the nominal robot model, physical tool, camera, payload and environment geometry against the installed cell. Review contact exclusions, clearances, link uncertainty and the tracking bound. Review the controller tool envelope and safety planes separately. Tool spheres and safety planes alone cannot detect tool-to-wrist mesh collisions. Keep planning on the Interface PC; no ROS runtime is proposed. See [UR Tool Position](https://www.universal-robots.com/manuals/EN/HTML/SW5_19/Content/prod-usr-man/software/PolyScope/content/safety_g5/Tool_position_g5_en.htm), [UR Safety Planes](https://www.universal-robots.com/manuals/EN/HTML/SW5_19/Content/prod-usr-man/complianceUR16e/H_g5_sections/firstuse/Planes_g5_en.htm), [FCL](https://github.com/flexible-collision-library/fcl), and the reference [anobot revision](https://github.com/khkoh23/anobot/tree/82206d330cf300db9b24aa9f24b2633a92e9db8f). Reference geometry needs measurement and review; do not copy its meshes here.
- Show both staged-motion endpoints before approval; record each stage's target, actual endpoint and status, including partial attempts.
- Make ChArUco test fixtures portable: three tests currently depend on an adjacent generator checkout.
- Review the teaching workspace minimum and hand-eye tilt and roll for each installation before use.
- Measure pick alignment independently and complete a full gripper pick/release cycle. Commanded-pose following alone cannot establish camera-to-item accuracy.
