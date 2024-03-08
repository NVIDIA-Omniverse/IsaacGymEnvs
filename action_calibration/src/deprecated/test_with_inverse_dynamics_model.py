# reference: https://github.com/vwxyzjn/cleanrl/blob/master/cleanrl/ppo_continuous_action_isaacgym/ppo_continuous_action_isaacgym.py

# Copyright (c) 2018-2022, NVIDIA Corporation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# docs and experiment results can be found at https://docs.cleanrl.dev/rl-algorithms/ppo/#ppo_continuous_action_isaacgympy
import os
import random
import time
from dataclasses import dataclass

import gym
import isaacgym  # noqa
import isaacgymenvs
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import tyro
from torch.distributions.normal import Normal
from torch.utils.tensorboard import SummaryWriter

from collections import deque


@dataclass
class Args:
    exp_name: str = os.path.basename(__file__)[: -len(".py")]
    """the name of this experiment"""
    seed: int = 100
    """seed of the experiment"""
    torch_deterministic: bool = True
    """if toggled, `torch.backends.cudnn.deterministic=False`"""
    cuda: bool = True
    """if toggled, cuda will be enabled by default"""
    track: bool = False
    """if toggled, this experiment will be tracked with Weights and Biases"""
    wandb_project_name: str = "cleanRL"
    """the wandb's project name"""
    wandb_entity: str = None
    """the entity (team) of wandb's project"""
    capture_video: bool = False
    """whether to capture videos of the agent performances (check out `videos` folder)"""

    # Algorithm specific arguments
    env_id: str = "Ant"
    """the id of the environment"""
    num_envs: int = 1024
    """the number of parallel game environments"""
    record_video_step_frequency: int = 100
    """the frequency at which to record the videos"""
    device_id: int = 7 # cwkang: set the gpu id
    """the gpu id"""
    len_history: int = 50
    """the frequency at which to record the videos"""

    # cwkang: Added for evaluation
    checkpoint_path: str = ""
    idm_checkpoint_path: str = ""
    """the path to the checkpoint"""

    # to be filled in runtime
    total_episodes: int = 1024 # cwkang: this value will be set the same as num_envs
    """total episodes for evaluation"""
    

class RecordEpisodeStatisticsTorch(gym.Wrapper):
    def __init__(self, env, device):
        super().__init__(env)
        self.num_envs = getattr(env, "num_envs", 1)
        self.device = device
        self.episode_returns = None
        self.episode_lengths = None

    def reset(self, **kwargs):
        observations = super().reset(**kwargs)
        self.episode_returns = torch.zeros(self.num_envs, dtype=torch.float32, device=self.device)
        self.episode_lengths = torch.zeros(self.num_envs, dtype=torch.int32, device=self.device)
        self.returned_episode_returns = torch.zeros(self.num_envs, dtype=torch.float32, device=self.device)
        self.returned_episode_lengths = torch.zeros(self.num_envs, dtype=torch.int32, device=self.device)
        return observations

    def step(self, action):
        observations, rewards, dones, infos = super().step(action)
        self.episode_returns += rewards
        self.episode_lengths += 1
        self.returned_episode_returns[:] = self.episode_returns
        self.returned_episode_lengths[:] = self.episode_lengths
        self.episode_returns *= 1 - dones
        self.episode_lengths *= 1 - dones
        infos["r"] = self.returned_episode_returns
        infos["l"] = self.returned_episode_lengths
        return (
            observations,
            rewards,
            dones,
            infos,
        )


def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer


class Agent(nn.Module):
    def __init__(self, envs):
        super().__init__()
        self.critic = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, 1), std=1.0),
        )
        self.actor_mean = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod(), 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, np.prod(envs.single_action_space.shape)), std=0.01),
        )
        self.actor_logstd = nn.Parameter(torch.zeros(1, np.prod(envs.single_action_space.shape)))

    def get_value(self, x):
        return self.critic(x)

    def get_action_and_value(self, x, action=None):
        action_mean = self.actor_mean(x)
        action_logstd = self.actor_logstd.expand_as(action_mean)
        action_std = torch.exp(action_logstd)
        probs = Normal(action_mean, action_std)
        if action is None:
            action = probs.sample()
        return action, probs.log_prob(action).sum(1), probs.entropy().sum(1), self.critic(x)


class InverseDynamicsModel(nn.Module):
    def __init__(self, envs):
        super().__init__()
        self.nn = nn.Sequential(
            layer_init(nn.Linear(np.array(envs.single_observation_space.shape).prod()*2, 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, 256)),
            nn.Tanh(),
            layer_init(nn.Linear(256, np.prod(envs.single_action_space.shape)), std=0.01),
        )

    def forward(self, x):
        return self.nn(x)


class ExtractObsWrapper(gym.ObservationWrapper):
    def observation(self, obs):
        return obs["obs"]


if __name__ == "__main__":
    args = tyro.cli(Args)
    args.total_episodes = args.num_envs
    # run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
    # run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{time.strftime('%Y-%m-%d_%H:%M:%S', time.localtime(time.time()))}" # cwkang: use datetime format for readability
    checkpoint_idx=os.path.basename(args.checkpoint_path).replace('.pth', '') # cwkang: add filename_suffix for tensorboard summarywriter
    seed_id = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(args.checkpoint_path))))
    source_env_id = os.path.basename(os.path.dirname(os.path.dirname(args.checkpoint_path)))
    run_name = f"test/{seed_id}/{os.path.join(args.env_id, 'idm_ac', checkpoint_idx)}"
    print(run_name)

    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")
    
    # env setup
    envs = isaacgymenvs.make(
        seed=args.seed,
        task=args.env_id,
        num_envs=args.num_envs,
        sim_device=f"cuda:{args.device_id}" if torch.cuda.is_available() and args.cuda else "cpu",
        rl_device=f"cuda:{args.device_id}" if torch.cuda.is_available() and args.cuda else "cpu",
        graphics_device_id=0 if torch.cuda.is_available() and args.cuda else -1,
        headless=False if torch.cuda.is_available() and args.cuda else True,
        multi_gpu=False,
        virtual_screen_capture=args.capture_video,
        force_render=False,
    )
    if args.capture_video:
        envs.is_vector_env = True
        print(f"record_video_step_frequency={args.record_video_step_frequency}")
        envs = gym.wrappers.RecordVideo(
            envs,
            f"videos/{run_name}",
            step_trigger=lambda step: step % args.record_video_step_frequency == 0,
            video_length=100,  # for each video record up to 100 steps
        )
    envs = ExtractObsWrapper(envs)
    envs = RecordEpisodeStatisticsTorch(envs, device)
    envs.single_action_space = envs.action_space
    envs.single_observation_space = envs.observation_space
    assert isinstance(envs.single_action_space, gym.spaces.Box), "only continuous action space is supported"

    agent = Agent(envs).to(device)
    agent.load_state_dict(torch.load(f'{args.checkpoint_path}'))
    agent.eval()

    # cwkang: load the inverse dynamics model
    idm = InverseDynamicsModel(envs).to(device)
    idm.load_state_dict(torch.load(f'{args.idm_checkpoint_path}'))
    idm.eval()
    mse_loss = nn.MSELoss()
    from sklearn.linear_model import LinearRegression
    # state_history = deque(maxlen=args.len_history)
    # next_state_history = deque(maxlen=args.len_history)
    action_history = deque(maxlen=args.len_history)
    predicted_action_history = deque(maxlen=args.len_history)
    compensation_matrix, compensation_bias = {}, {}

    # TRY NOT TO MODIFY: start the game
    global_step = 0
    start_time = time.time()
    next_obs = envs.reset()
    next_done = torch.zeros(args.num_envs, dtype=torch.float).to(device)

    from collections import defaultdict
    test_results = defaultdict(dict) # cwkang: record test results
    num_episodes = 0

    while num_episodes < args.total_episodes:
        global_step += args.num_envs

        # ALGO LOGIC: action logic
        with torch.no_grad():
            action, logprob, _, value = agent.get_action_and_value(next_obs)
            action = torch.clamp(action, -envs.clip_actions, envs.clip_actions) # cwkang: clip action for accurate prediction without noise
            # TODO: do something here (action compensation) before history append
            if len(compensation_matrix) > 0:
                # print(action[0])
                for i in compensation_matrix:
                    compensation_weight = 0.2
                    compensated_action = action[i] @ compensation_matrix[i].T + compensation_bias[i]
                    action[i] = (1-compensation_weight)*action[i] + compensation_weight*compensated_action
                action = torch.clamp(action, -envs.clip_actions, envs.clip_actions)
                # print(compensation_matrix[i].T)
                # print(compensation_bias[i])
                # print(action[0])
                # print()
            current_state = next_obs.clone() # cwkang: save current state
            action_history.append(action.detach().cpu().numpy())

        # TRY NOT TO MODIFY: execute the game and log data.
        next_obs, reward, next_done, info = envs.step(action)
        idm_input = torch.cat((current_state, next_obs), dim=-1)
        predicted_action = idm(idm_input)
        predicted_action_history.append(predicted_action.detach().cpu().numpy())

        X = np.array(predicted_action_history, dtype=np.float32)
        Y = np.array(action_history, dtype=np.float32)
        for i in range(args.num_envs):
            X_i = X[:,i,:]
            Y_i = Y[:,i,:]
            X_augmented = np.hstack([X_i, np.ones((X_i.shape[0], 1))])
            params, residuals, rank, s = np.linalg.lstsq(X_augmented, Y_i, rcond=None)
            A, b = params[:-1], params[-1]
            # reg = LinearRegression()
            # reg.fit(X_i, Y_i)
            # A, b = reg.coef_, reg.intercept_
            compensation_matrix[i] = torch.FloatTensor(A).cuda(args.device_id)
            compensation_bias[i] = torch.FloatTensor(b).cuda(args.device_id)
        
        for idx, d in enumerate(next_done):
            if d:
                if idx in test_results['episodic_return']: # cwkang: one environment produces one result for fair comparison (evaluation is done with the same initial states)
                    continue

                episodic_return = info["r"][idx].item()
                episodic_length = info["l"][idx].item()
                test_results['episodic_return'][idx] = episodic_return # cwkang: record results
                test_results['episodic_length'][idx] = episodic_length # cwkang: record results

                if "consecutive_successes" in info:  # ShadowHand and AllegroHand metric
                    consecutive_successes = info["consecutive_successes"].item()
                    test_results['consecutive_successes'][idx] = consecutive_successes # cwkang: record results

                num_episodes = len(test_results['episodic_return']) # cwkang: count the number of episodes for recording
                if num_episodes % (args.total_episodes // 10) == 0 or num_episodes == args.total_episodes:
                    print(f"{num_episodes} episodes done")

                if num_episodes == args.total_episodes:
                    break
        
        # print("SPS:", int(global_step / (time.time() - start_time)))
        writer.add_scalar(f"evaluation_seed_{args.seed}/SPS", int(global_step / (time.time() - start_time)), global_step)

    for key in test_results:
        for idx in sorted(list(test_results[key].keys())):
            writer.add_scalar(f"evaluation_seed_{args.seed}/{key}", test_results[key][idx], idx)

    # TRY NOT TO MODIFY: record rewards for plotting purposes
    print()
    for key in test_results:
        test_results[key] = list(test_results[key].values())
    print(f"episodic_return: mean={np.mean(test_results['episodic_return'])}, std={np.std(test_results['episodic_return'])}")
    print(f"episodic_length: mean={np.mean(test_results['episodic_length'])}, std={np.std(test_results['episodic_length'])}")
    writer.add_scalar(f"evaluation_seed_{args.seed}/episodic_return_mean", np.mean(test_results['episodic_return']), checkpoint_idx)
    writer.add_scalar(f"evaluation_seed_{args.seed}/episodic_return_std", np.std(test_results['episodic_return']), checkpoint_idx)
    writer.add_scalar(f"evaluation_seed_{args.seed}/episodic_length_mean", np.mean(test_results['episodic_length']), checkpoint_idx)
    writer.add_scalar(f"evaluation_seed_{args.seed}/episodic_length_std", np.std(test_results['episodic_length']), checkpoint_idx)
    if 'consecutive_successes' in test_results:
        print(f"consecutive_successes: mean={np.mean(test_results['consecutive_successes'])}, std={np.std(test_results['consecutive_successes'])}")
        writer.add_scalar(f"evaluation_seed_{args.seed}/consecutive_successes_mean", np.mean(test_results['consecutive_successes']), checkpoint_idx)
        writer.add_scalar(f"evaluation_seed_{args.seed}/consecutive_successes_std", np.std(test_results['consecutive_successes']), checkpoint_idx)


    # envs.close()
    writer.close()