# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import dataclasses

import hydra
import torch.cuda
from hydra.core.config_store import ConfigStore
from torchrl.envs import EnvCreator, ParallelEnv
from torchrl.envs.transforms import RewardScaling, TransformedEnv
from torchrl.envs.utils import set_exploration_mode
from torchrl.objectives.value import GAE
from torchrl.record import VideoRecorder
from torchrl.trainers.helpers.collectors import (
    make_collector_onpolicy,
    OnPolicyCollectorConfig,
)
from torchrl.trainers.helpers.envs import (
    correct_for_frame_skip,
    EnvConfig,
    initialize_observation_norm_transforms,
    parallel_env_constructor,
    retrieve_observation_norms_state_dict,
    transformed_env_constructor,
)
from torchrl.trainers.helpers.logger import LoggerConfig
from torchrl.trainers.helpers.losses import make_ppo_loss, PPOLossConfig
from torchrl.trainers.helpers.models import make_ppo_model, PPOModelConfig
from torchrl.trainers.helpers.trainers import make_trainer, TrainerConfig
from torchrl.trainers.loggers.utils import generate_exp_name, get_logger

config_fields = [
    (config_field.name, config_field.type, config_field)
    for config_cls in (
        TrainerConfig,
        OnPolicyCollectorConfig,
        EnvConfig,
        PPOLossConfig,
        PPOModelConfig,
        LoggerConfig,
    )
    for config_field in dataclasses.fields(config_cls)
]

Config = dataclasses.make_dataclass(cls_name="Config", fields=config_fields)
cs = ConfigStore.instance()
cs.store(name="config", node=Config)


@hydra.main(version_base=None, config_path=".", config_name="config")
def main(cfg: "DictConfig"):  # noqa: F821

    cfg = correct_for_frame_skip(cfg)

    if not isinstance(cfg.reward_scaling, float):
        cfg.reward_scaling = 1.0

    device = (
        torch.device("cpu")
        if torch.cuda.device_count() == 0
        else torch.device("cuda:0")
    )

    exp_name = generate_exp_name("PPO", cfg.exp_name)
    logger = get_logger(
        logger_type=cfg.logger, logger_name="ppo_logging", experiment_name=exp_name
    )
    video_tag = exp_name if cfg.record_video else ""

    key, init_env_steps, stats = None, None, None
    if not cfg.vecnorm and cfg.norm_stats:
        if not hasattr(cfg, "init_env_steps"):
            raise AttributeError("init_env_steps missing from arguments.")
        key = ("next", "pixels") if cfg.from_pixels else ("next", "observation_vector")
        init_env_steps = cfg.init_env_steps
        stats = {"loc": None, "scale": None}
    elif cfg.from_pixels:
        stats = {"loc": 0.5, "scale": 0.5}

    proof_env = transformed_env_constructor(
        cfg=cfg,
        use_env_creator=False,
        stats=stats,
    )()
    initialize_observation_norm_transforms(
        proof_environment=proof_env, num_iter=init_env_steps, key=key
    )
    _, obs_norm_state_dict = retrieve_observation_norms_state_dict(proof_env)[0]

    model = make_ppo_model(
        proof_env,
        cfg=cfg,
        device=device,
    )
    actor_model = model.get_policy_operator()

    loss_module = make_ppo_loss(model, cfg)
    if cfg.gSDE:
        with torch.no_grad(), set_exploration_mode("random"):
            # get dimensions to build the parallel env
            proof_td = model(proof_env.reset().to(device))
        action_dim_gsde, state_dim_gsde = proof_td.get("_eps_gSDE").shape[-2:]
        del proof_td
    else:
        action_dim_gsde, state_dim_gsde = None, None

    proof_env.close()
    create_env_fn = parallel_env_constructor(
        cfg=cfg,
        obs_norm_state_dict=obs_norm_state_dict,
        action_dim_gsde=action_dim_gsde,
        state_dim_gsde=state_dim_gsde,
    )

    collector = make_collector_onpolicy(
        make_env=create_env_fn,
        actor_model_explore=actor_model,
        cfg=cfg,
        # make_env_kwargs=[
        #     {"device": device} if device >= 0 else {}
        #     for device in cfg.env_rendering_devices
        # ],
    )

    recorder = transformed_env_constructor(
        cfg,
        video_tag=video_tag,
        norm_obs_only=True,
        obs_norm_state_dict=obs_norm_state_dict,
        logger=logger,
        use_env_creator=False,
    )()

    # remove video recorder from recorder to have matching state_dict keys
    if cfg.record_video:
        recorder_rm = TransformedEnv(recorder.base_env)
        for transform in recorder.transform:
            if not isinstance(transform, VideoRecorder):
                recorder_rm.append_transform(transform.clone())
    else:
        recorder_rm = recorder

    if isinstance(create_env_fn, ParallelEnv):
        recorder_rm.load_state_dict(create_env_fn.state_dict()["worker0"])
        create_env_fn.close()
    elif isinstance(create_env_fn, EnvCreator):
        recorder_rm.load_state_dict(create_env_fn().state_dict())
    else:
        recorder_rm.load_state_dict(create_env_fn.state_dict())

    # reset reward scaling
    for t in recorder.transform:
        if isinstance(t, RewardScaling):
            t.scale.fill_(1.0)
            t.loc.fill_(0.0)

    trainer = make_trainer(
        collector,
        loss_module,
        recorder,
        None,
        actor_model,
        None,
        logger,
        cfg,
    )
    if cfg.loss == "kl":
        trainer.register_op("pre_optim_steps", loss_module.reset)

    if not cfg.advantage_in_loss:
        critic_model = model.get_value_operator()
        advantage = GAE(
            cfg.gamma,
            cfg.lmbda,
            value_network=critic_model,
            average_rewards=True,
            gradient_mode=False,
        )
        trainer.register_op(
            "process_optim_batch",
            advantage,
        )
        trainer._process_optim_batch_ops = [
            trainer._process_optim_batch_ops[-1],
            *trainer._process_optim_batch_ops[:-1],
        ]

    final_seed = collector.set_seed(cfg.seed)
    print(f"init seed: {cfg.seed}, final seed: {final_seed}")

    trainer.train()
    return (logger.log_dir, trainer._log_dict)


if __name__ == "__main__":
    main()
