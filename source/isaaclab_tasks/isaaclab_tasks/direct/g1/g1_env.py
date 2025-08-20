# Copyright (c) 2022-2025, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import gymnasium as gym
import torch
import math
import isaaclab.utils.math as math_utils

import isaaclab.sim as sim_utils
from isaaclab.assets import Articulation
from isaaclab.envs import DirectRLEnv
from isaaclab.sensors import ContactSensor, RayCaster

from .g1_env_cfg import G1FlatEnvCfg, G1RoughEnvCfg


class G1Env(DirectRLEnv):
    cfg: G1FlatEnvCfg | G1RoughEnvCfg

    def __init__(self, cfg: G1FlatEnvCfg | G1RoughEnvCfg, render_mode: str | None = None, **kwargs):
        super().__init__(cfg, render_mode, **kwargs)

        # Joint position command (deviation from default joint positions)
        self._actions = torch.zeros(self.num_envs, gym.spaces.flatdim(self.single_action_space), device=self.device)
        self._previous_actions = torch.zeros(
            self.num_envs, gym.spaces.flatdim(self.single_action_space), device=self.device
        )

        # X/Y linear velocity and yaw heading commands
        self._commands = torch.zeros(self.num_envs, 3, device=self.device)

        # Logging
        self._episode_sums = {
            key: torch.zeros(self.num_envs, dtype=torch.float, device=self.device)
            for key in [
                "track_lin_vel_xy_exp",
                "track_ang_vel_z_exp",
                "feet_air_time",
                "feet_slide",
                "dof_pos_limits",
                "joint_deviation_hip",
                "joint_deviation_arms",
                "joint_deviation_fingers",
                "joint_deviation_torso",
                "lin_vel_z_l2",
                "ang_vel_xy_l2",
                "dof_torques_l2",
                "dof_acc_l2",
                "action_rate_l2",
                "flat_orientation_l2",
            ]
        }

        # Get specific body indices
        self._base_id, _ = self._contact_sensor.find_bodies("pelvis")
        self._feet_ids, _ = self._contact_sensor.find_bodies(".*_ankle_roll_link")
        self._ankle_ids, _ = self._robot.find_joints([".*_ankle_pitch_joint", ".*_ankle_roll_joint"])
        self._hip_ids, _ = self._robot.find_joints([".*_hip_yaw_joint", ".*_hip_roll_joint"])
        self._knee_ids, _ = self._robot.find_joints([".*_knee_joint"])
        self._arm_ids, _ = self._robot.find_joints([".*_shoulder_pitch_joint", ".*_shoulder_roll_joint", ".*_shoulder_yaw_joint", ".*_elbow_pitch_joint", ".*_elbow_roll_joint"])
        self._finger_ids, _ = self._robot.find_joints([".*_five_joint", ".*_three_joint", ".*_six_joint", ".*_four_joint", ".*_zero_joint", ".*_one_joint", ".*_two_joint"])
        self._torso_ids, _ = self._robot.find_joints(["torso_joint"])

    def _setup_scene(self):
        self._robot = Articulation(self.cfg.robot)
        self.scene.articulations["robot"] = self._robot
        self._contact_sensor = ContactSensor(self.cfg.contact_sensor)
        self.scene.sensors["contact_sensor"] = self._contact_sensor
        if isinstance(self.cfg, G1RoughEnvCfg):
            # we add a height scanner for perceptive locomotion
            self._height_scanner = RayCaster(self.cfg.height_scanner)
            self.scene.sensors["height_scanner"] = self._height_scanner
        self.cfg.terrain.num_envs = self.scene.cfg.num_envs
        self.cfg.terrain.env_spacing = self.scene.cfg.env_spacing
        self._terrain = self.cfg.terrain.class_type(self.cfg.terrain)
        # clone and replicate
        self.scene.clone_environments(copy_from_source=False)
        # add lights
        light_cfg = sim_utils.DomeLightCfg(intensity=2000.0, color=(0.75, 0.75, 0.75))
        light_cfg.func("/World/Light", light_cfg)

    def _pre_physics_step(self, actions: torch.Tensor):
        self._actions = actions.clone()
        self._processed_actions = self.cfg.action_scale * self._actions + self._robot.data.default_joint_pos

    def _apply_action(self):
        self._robot.set_joint_position_target(self._processed_actions)

    def _get_observations(self) -> dict:
        self._previous_actions = self._actions.clone()
        height_data = None
        if isinstance(self.cfg, G1RoughEnvCfg):
            height_data = (
                self._height_scanner.data.pos_w[:, 2].unsqueeze(1) - self._height_scanner.data.ray_hits_w[..., 2] - 0.5
            ).clip(-1.0, 1.0)
        obs = torch.cat(
            [
                tensor
                for tensor in (
                    self._robot.data.root_lin_vel_b,
                    self._robot.data.root_ang_vel_b,
                    self._robot.data.projected_gravity_b,
                    self._commands,
                    self._robot.data.joint_pos - self._robot.data.default_joint_pos,
                    self._robot.data.joint_vel,
                    height_data,
                    self._actions,
                )
                if tensor is not None
            ],
            dim=-1,
        )
        observations = {"policy": obs}
        return observations

    def _get_rewards(self) -> torch.Tensor:

        ### Convert command heading position to angular velocity
        heading_error = math_utils.wrap_to_pi(self._commands[:, 2] - self._robot.data.heading_w)
        self._commands[:, 2] = torch.clip(
            1.0 * heading_error,
            min=-1.0,
            max=1.0,
        )

        # linear velocity tracking
        lin_vel_error = torch.sum(torch.square(self._commands[:, :2] - self._robot.data.root_lin_vel_b[:, :2]), dim=1)
        lin_vel_error_mapped = torch.exp(-lin_vel_error / 0.25)
        # yaw rate tracking
        yaw_rate_error = torch.square(self._commands[:, 2] - self._robot.data.root_ang_vel_b[:, 2])
        yaw_rate_error_mapped = torch.exp(-yaw_rate_error / 0.25)
        # z velocity tracking
        z_vel_error = torch.square(self._robot.data.root_lin_vel_b[:, 2])
        # angular velocity x/y
        ang_vel_error = torch.sum(torch.square(self._robot.data.root_ang_vel_b[:, :2]), dim=1)
        # joint torques
        joint_torques = torch.sum(torch.square(self._robot.data.applied_torque[:, self._hip_ids + self._knee_ids]), dim=1)
        # joint acceleration
        joint_accel = torch.sum(torch.square(self._robot.data.joint_acc), dim=1)
        # action rate
        action_rate = torch.sum(torch.square(self._actions - self._previous_actions), dim=1)
        # feet air time
        first_contact = self._contact_sensor.compute_first_contact(self.step_dt)[:, self._feet_ids]
        last_air_time = self._contact_sensor.data.last_air_time[:, self._feet_ids]

        feet_air_time_threshold = 0.4
        air_time = torch.sum((last_air_time - feet_air_time_threshold) * first_contact, dim=1) * (
            torch.norm(self._commands[:, :2], dim=1) > 0.1
        )

        # feet slide
        contacts = self._contact_sensor.data.net_forces_w_history[:, :, self._feet_ids, :].norm(dim=-1).max(dim=1)[0] > 1.0
        body_vel = self._robot.data.body_lin_vel_w[:, self._base_id, :2]
        feet_slide = torch.sum(body_vel.norm(dim=-1) * contacts, dim=1)

        # joint pos limits
        out_of_limits = -(
            self._robot.data.joint_pos[:, self._ankle_ids] - self._robot.data.soft_joint_pos_limits[:, self._ankle_ids, 0]
        ).clip(max=0.0)
        out_of_limits += (
            self._robot.data.joint_pos[:, self._ankle_ids] - self._robot.data.soft_joint_pos_limits[:, self._ankle_ids, 1]
        ).clip(min=0.0)
        joint_pos_limits = torch.sum(out_of_limits, dim=1)

        # joint deviation l1
        hip_angle = self._robot.data.joint_pos[:, self._hip_ids] - self._robot.data.default_joint_pos[:, self._hip_ids]
        arms_angle = self._robot.data.joint_pos[:, self._arm_ids] - self._robot.data.default_joint_pos[:, self._arm_ids]
        fingers_angle = self._robot.data.joint_pos[:, self._finger_ids] - self._robot.data.default_joint_pos[:, self._finger_ids]
        torso_angle = self._robot.data.joint_pos[:, self._torso_ids] - self._robot.data.default_joint_pos[:, self._torso_ids]
        
        joint_deviation_hip = torch.sum(torch.abs(hip_angle), dim=1)
        joint_deviation_arms = torch.sum(torch.abs(arms_angle), dim=1)
        joint_deviation_fingers = torch.sum(torch.abs(fingers_angle), dim=1)
        joint_deviation_torso = torch.sum(torch.abs(torso_angle), dim=1)

        # flat orientation
        flat_orientation = torch.sum(torch.square(self._robot.data.projected_gravity_b[:, :2]), dim=1)

        rewards = {
            # "termination_penalty":
            "track_lin_vel_xy_exp": lin_vel_error_mapped * self.cfg.track_lin_vel_xy_exp_scale * self.step_dt,
            "track_ang_vel_z_exp": yaw_rate_error_mapped * self.cfg.track_ang_vel_z_exp_scale * self.step_dt,
            "feet_air_time": air_time * self.cfg.feet_air_time_reward_scale * self.step_dt,
            "feet_slide": feet_slide * self.cfg.feet_slide_reward_scale * self.step_dt,
            "dof_pos_limits": joint_pos_limits * self.cfg.dof_pos_limit_reward_scale * self.step_dt,
            "joint_deviation_hip": joint_deviation_hip * self.cfg.joint_deviation_hip_reward_scale * self.step_dt,
            "joint_deviation_arms": joint_deviation_arms * self.cfg.joint_deviation_arms_reward_scale * self.step_dt,
            "joint_deviation_fingers": joint_deviation_fingers * self.cfg.joint_deviation_fingers_reward_scale * self.step_dt,
            "joint_deviation_torso": joint_deviation_torso * self.cfg.joint_deviation_torso_reward_scale * self.step_dt,
            
            # other rewards
            "lin_vel_z_l2": z_vel_error * self.cfg.lin_vel_z_l2_scale * self.step_dt,
            "ang_vel_xy_l2": ang_vel_error * self.cfg.ang_vel_reward_scale * self.step_dt,
            "dof_torques_l2": joint_torques * self.cfg.joint_torque_reward_scale * self.step_dt,
            "dof_acc_l2": joint_accel * self.cfg.joint_accel_reward_scale * self.step_dt,
            "action_rate_l2": action_rate * self.cfg.action_rate_reward_scale * self.step_dt,
            "flat_orientation_l2": flat_orientation * self.cfg.flat_orientation_reward_scale * self.step_dt,
        }

        reward = torch.sum(torch.stack(list(rewards.values())), dim=0)


        # print("lin_vel_error_mapped", lin_vel_error)
        # print("yaw_rate_error_mapped", yaw_rate_error)
        # Logging
        for key, value in rewards.items():
            self._episode_sums[key] += value
        return reward

    def _get_dones(self) -> tuple[torch.Tensor, torch.Tensor]:
        time_out = self.episode_length_buf >= self.max_episode_length - 1
        net_contact_forces = self._contact_sensor.data.net_forces_w_history
        died = torch.any(torch.max(torch.norm(net_contact_forces[:, :, self._base_id], dim=-1), dim=1)[0] > 1.0, dim=1)
        return died, time_out

    def _reset_idx(self, env_ids: torch.Tensor | None):
        if env_ids is None or len(env_ids) == self.num_envs:
            env_ids = self._robot._ALL_INDICES
        self._robot.reset(env_ids)
        super()._reset_idx(env_ids)
        if len(env_ids) == self.num_envs:
            # Spread out the resets to avoid spikes in training when many environments reset at a similar time
            self.episode_length_buf[:] = torch.randint_like(self.episode_length_buf, high=int(self.max_episode_length))
        self._actions[env_ids] = 0.0
        self._previous_actions[env_ids] = 0.0
        # Sample new commands

        # command: 
        # self._commands[env_ids] = torch.zeros_like(self._commands[env_ids]).uniform_(-1.0, 1.0)        
        # self._commands[env_ids, 2] = self._commands[env_ids, 2] * math.pi

        self._commands[env_ids, 0] = torch.zeros_like(self._commands[env_ids, 0])
        self._commands[env_ids, 0] = 1
        # Reset robot state
        joint_pos = self._robot.data.default_joint_pos[env_ids]
        joint_vel = self._robot.data.default_joint_vel[env_ids]
        default_root_state = self._robot.data.default_root_state[env_ids]
        default_root_state[:, :3] += self._terrain.env_origins[env_ids]
        self._robot.write_root_pose_to_sim(default_root_state[:, :7], env_ids)
        self._robot.write_root_velocity_to_sim(default_root_state[:, 7:], env_ids)
        self._robot.write_joint_state_to_sim(joint_pos, joint_vel, None, env_ids)
        # Logging
        extras = dict()
        for key in self._episode_sums.keys():
            episodic_sum_avg = torch.mean(self._episode_sums[key][env_ids])
            extras["Episode_Reward/" + key] = episodic_sum_avg / self.max_episode_length_s
            self._episode_sums[key][env_ids] = 0.0
        self.extras["log"] = dict()
        self.extras["log"].update(extras)
        extras = dict()
        extras["Episode_Termination/base_contact"] = torch.count_nonzero(self.reset_terminated[env_ids]).item()
        extras["Episode_Termination/time_out"] = torch.count_nonzero(self.reset_time_outs[env_ids]).item()
        self.extras["log"].update(extras)
