#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Train and Eval SAC.
To run:
```bash
tensorboard --logdir $HOME/tmp/sac_v1/gym/HalfCheetah-v2/ --port 2223 &
python tf_agents/agents/sac/examples/v1/train_eval.py \
  --root_dir=$HOME/tmp/sac_v1/gym/HalfCheetah-v2/ \
  --alsologtostderr
```
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time
import matplotlib
matplotlib.use("pdf")
import matplotlib.pyplot as plt
from absl import app
from absl import flags
from absl import logging

import gin
import tensorflow as tf

from tf_agents.agents.ddpg import critic_network
import latent_agent
from tf_agents.agents.sac import sac_agent
from tf_agents.drivers import dynamic_step_driver
# from tf_agents.environments import suite_mujoco
from tf_agents.environments import suite_gym
from tf_agents.environments import tf_py_environment
from tf_agents.eval import metric_utils
from tf_agents.metrics import py_metrics
from tf_agents.metrics import tf_metrics
from tf_agents.metrics import tf_py_metric
import latent_actor_network
import latent_action_generator
from tf_agents.networks import normal_projection_network
from tf_agents.policies import greedy_policy
from tf_agents.policies import py_tf_policy
from tf_agents.policies import random_tf_policy
from tf_agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.utils import common
from tf_agents.specs import tensor_spec
from rlkit.envs import ENVS
from rlkit.envs.wrappers import NormalizedBoxEnv
import gym_backcheetah
import gym_cheetahvel
import gym

flags.DEFINE_string('root_dir', os.getenv('TEST_UNDECLARED_OUTPUTS_DIR'),
                    'Root directory for writing logs/summaries/checkpoints.')
flags.DEFINE_multi_string('gin_file', None,
                          'Path to the gin config files.')
flags.DEFINE_multi_string('gin_param', None, 'Gin binding to pass through.')
flags.DEFINE_bool('finetune', False, 'flag to specify finetuning')
flags.DEFINE_bool('direc', False, 'flag to specify special envs setup')

FLAGS = flags.FLAGS
Z_DIM = 16

@gin.configurable
def normal_projection_net(action_spec,
                          init_action_stddev=0.35,
                          init_means_output_factor=0.1):
  del init_action_stddev
  return normal_projection_network.NormalProjectionNetwork(
      action_spec,
      mean_transform=None,
      state_dependent_std=True,
      init_means_output_factor=init_means_output_factor,
      std_transform=latent_agent.std_clip_transform,
      scale_distribution=True)


@gin.configurable
def train_eval(
    root_dir,
    finetune,
    direc,
    env_name='CheetahVel-v0',
    eval_env_name=None,
    env_load_fn=suite_gym.load,
    train_tasks=10,
    num_iterations=3000000,
    actor_fc_layers=(256, 256, 16),
    critic_obs_fc_layers=None,
    critic_action_fc_layers=None,
    critic_joint_fc_layers=(256, 256),
    # Params for collect
    initial_collect_steps=10000,
    collect_steps_per_iteration=1,
    replay_buffer_capacity=1000000,
    # Params for target update
    target_update_tau=0.005,
    target_update_period=1,
    # Params for train
    train_steps_per_iteration=1,
    batch_size=256,
    actor_learning_rate=3e-4,
    critic_learning_rate=3e-4,
    alpha_learning_rate=3e-4,
    td_errors_loss_fn=tf.compat.v1.losses.mean_squared_error,
    gamma=0.99,
    reward_scale_factor=1.0,
    gradient_clipping=None,
    # Params for eval
    num_eval_episodes=30,
    eval_interval=10000,
    # Params for summaries and logging
    train_checkpoint_interval=100000,
    policy_checkpoint_interval=100000,
    rb_checkpoint_interval=100000,
    log_interval=1000,
    plot_interval=100000,
    summary_interval=1000,
    summaries_flush_secs=10,
    debug_summaries=False,
    summarize_grads_and_vars=False,
    eval_metrics_callback=None):

  """A simple train and eval for SAC."""
  root_dir = os.path.expanduser(root_dir)
  train_dir = os.path.join(root_dir, 'train')
  eval_dir = os.path.join(root_dir, 'eval')

  train_summary_writer = tf.compat.v2.summary.create_file_writer(
      train_dir, flush_millis=summaries_flush_secs * 1000)
  train_summary_writer.set_as_default()

  eval_summary_writer = tf.compat.v2.summary.create_file_writer(
      eval_dir, flush_millis=summaries_flush_secs * 1000)
  eval_metrics = [
      py_metrics.AverageReturnMetric(buffer_size=num_eval_episodes),
      py_metrics.AverageEpisodeLengthMetric(buffer_size=num_eval_episodes),
  ]
  eval_summary_flush_op = eval_summary_writer.flush()

  global_step = tf.compat.v1.train.get_or_create_global_step()
  with tf.compat.v2.summary.record_if(
      lambda: tf.math.equal(global_step % summary_interval, 0)):
    # Create the environment.
    # env_params = {"n_tasks": 2}
    # env = NormalizedBoxEnv(ENVS["cheetah-dir"](env_params))
    # tf_env_2 = tf_py_environment.TFPyEnvironment(env)
    tf_env = {}
    eval_py_env = {}

    if direc:
      loaded_env_1 = env_load_fn("HalfCheetah-v2")
      loaded_env_2 = env_load_fn("BackCheetah-v0")
      eval_py_env[0] = loaded_env_1
      eval_py_env[1] = loaded_env_2
      tf_env[0] = tf_py_environment.TFPyEnvironment(loaded_env_1)
      tf_env[1] = tf_py_environment.TFPyEnvironment(loaded_env_2)
      train_tasks = 2

    else:
      for idx in range(train_tasks):
        loaded_env = env_load_fn(env_name)
        eval_py_env[idx] = loaded_env
        tf_env[idx] = tf_py_environment.TFPyEnvironment(loaded_env)
  
    # Get the data specs from the environment
    time_step_spec = tf_env[0].time_step_spec()
    observation_spec = time_step_spec.observation
    action_spec = tf_env[0].action_spec()

    print("Initializing actor network")
    z_spec = tensor_spec.TensorSpec(shape=[Z_DIM], dtype=tf.dtypes.float64, name='z')
    action_generator = latent_action_generator.ActionGenerator(input_tensor_spec=(time_step_spec.observation, z_spec), 
      output_tensor_spec=action_spec)
    action_generator.create_variables()
    actor_net = latent_actor_network.ActorDistributionNetwork(
        observation_spec,
        action_spec,
        fc_layer_params=actor_fc_layers,
        continuous_projection_net=normal_projection_net,
        action_generator=action_generator)
    critic_net = critic_network.CriticNetwork(
        (observation_spec, action_spec),
        observation_fc_layer_params=critic_obs_fc_layers,
        action_fc_layer_params=critic_action_fc_layers,
        joint_fc_layer_params=critic_joint_fc_layers)
    print("Initializing latent agent")
    agent = latent_agent.SacAgent(
        time_step_spec,
        action_spec,
        finetune,
        actor_network=actor_net,
        action_generator=action_generator,
        critic_network=critic_net,
        actor_optimizer=tf.compat.v1.train.AdamOptimizer(
            learning_rate=actor_learning_rate),
        critic_optimizer=tf.compat.v1.train.AdamOptimizer(
            learning_rate=critic_learning_rate),
        alpha_optimizer=tf.compat.v1.train.AdamOptimizer(
            learning_rate=alpha_learning_rate),
        target_update_tau=target_update_tau,
        target_update_period=target_update_period,
        td_errors_loss_fn=td_errors_loss_fn,
        gamma=gamma,
        reward_scale_factor=reward_scale_factor,
        gradient_clipping=gradient_clipping,
        debug_summaries=debug_summaries,
        summarize_grads_and_vars=summarize_grads_and_vars,
        train_step_counter=global_step)

    # Make the replay buffer.
    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        data_spec=agent.collect_data_spec,
        batch_size=1,
        max_length=replay_buffer_capacity)
    replay_observer = [replay_buffer.add_batch]

    eval_py_policy = py_tf_policy.PyTFPolicy(
        greedy_policy.GreedyPolicy(agent.policy))

    train_metrics = [
        tf_metrics.NumberOfEpisodes(),
        tf_metrics.EnvironmentSteps(),
        tf_py_metric.TFPyMetric(py_metrics.AverageReturnMetric()),
        tf_py_metric.TFPyMetric(py_metrics.AverageEpisodeLengthMetric()),
    ]

    collect_policy = agent.collect_policy
    initial_collect_policy = random_tf_policy.RandomTFPolicy(
        tf_env[0].time_step_spec(), tf_env[0].action_spec())

    initial_collect_op = {}
    for idx in range(train_tasks):
      initial_collect_op[idx] = dynamic_step_driver.DynamicStepDriver(
          tf_env[idx],
          initial_collect_policy,
          observers=replay_observer + train_metrics,
          num_steps=initial_collect_steps).run()

    collect_op = {}
    for idx in range(train_tasks):
      collect_op[idx] = dynamic_step_driver.DynamicStepDriver(
          tf_env[idx],
          collect_policy,
          observers=replay_observer + train_metrics,
          num_steps=collect_steps_per_iteration).run()

    # Prepare replay buffer as dataset with invalid transitions filtered.
    def _filter_invalid_transition(trajectories, unused_arg1):
      return ~trajectories.is_boundary()[0]
    dataset = replay_buffer.as_dataset(
        sample_batch_size=5 * batch_size,
        num_steps=2).unbatch().filter(
            _filter_invalid_transition).batch(batch_size).prefetch(
                batch_size * 5)
    dataset_iterator = tf.compat.v1.data.make_initializable_iterator(dataset)
    trajectories, unused_info = dataset_iterator.get_next()
    train_op = agent.train(trajectories)

    summary_ops = []
    for train_metric in train_metrics:
      summary_ops.append(train_metric.tf_summaries(
          train_step=global_step, step_metrics=train_metrics[:2]))

    with eval_summary_writer.as_default(), \
         tf.compat.v2.summary.record_if(True):
      for eval_metric in eval_metrics:
        eval_metric.tf_summaries(train_step=global_step)

    train_checkpointer = common.Checkpointer(
        ckpt_dir=train_dir,
        agent=agent,
        global_step=global_step,
        metrics=metric_utils.MetricsGroup(train_metrics, 'train_metrics'))
    policy_checkpointer = common.Checkpointer(
        ckpt_dir=os.path.join(train_dir, 'policy'),
        policy=agent.policy,
        global_step=global_step)
    rb_checkpointer = common.Checkpointer(
        ckpt_dir=os.path.join(train_dir, 'replay_buffer'),
        max_to_keep=1,
        replay_buffer=replay_buffer)

    with tf.compat.v1.Session() as sess:
      # Initialize graph.
      train_checkpointer.initialize_or_restore(sess)
      rb_checkpointer.initialize_or_restore(sess)

      # Initialize training.
      sess.run(dataset_iterator.initializer)
      common.initialize_uninitialized_variables(sess)
      sess.run(train_summary_writer.init())
      sess.run(eval_summary_writer.init())

      global_step_val = sess.run(global_step)

      if global_step_val == 0:
        # Initial eval of randomly initialized policy
        if finetune:
          for idx in range(train_tasks):
            metric_utils.compute_summaries(
                eval_metrics,
                eval_py_env[idx],
                eval_py_policy,
                num_episodes=num_eval_episodes,
                global_step=global_step_val,
                callback=eval_metrics_callback,
                log=True,
            )
            sess.run(eval_summary_flush_op)

        # Run initial collect.
        logging.info('Global step %d: Running initial collect op.',
                     global_step_val) 

        for idx in range(train_tasks):
          sess.run(initial_collect_op[idx])


        # Checkpoint the initial replay buffer contents.
        rb_checkpointer.save(global_step=global_step_val)

        logging.info('Finished initial collect.')
      else:
        logging.info('Global step %d: Skipping initial collect op.',
                     global_step_val)

      collect_call = {}
      for idx in range(train_tasks):
        collect_call[idx] = sess.make_callable(collect_op[idx])
      train_step_call = sess.make_callable([train_op, summary_ops])
      global_step_call = sess.make_callable(global_step)

      timed_at_step = global_step_call()
      time_acc = 0
      steps_per_second_ph = tf.compat.v1.placeholder(
          tf.float32, shape=(), name='steps_per_sec_ph')
      steps_per_second_summary = tf.compat.v2.summary.scalar(
          name='global_steps_per_sec', data=steps_per_second_ph,
          step=global_step)
      
      returnsCache = []
      for _ in range(num_iterations):
        start_time = time.time()
        for idx in range(train_tasks):
          collect_call[idx]()
        for _ in range(train_steps_per_iteration):
          total_loss, _ = train_step_call()
        time_acc += time.time() - start_time
        global_step_val = global_step_call()
        if global_step_val % log_interval == 0:
          # info_buffer = agent.get_info()
          # tf.print(info_buffer["vae_loss"])
          # raise NotImplementedError
          logging.info('step = %d, loss = %f', global_step_val, total_loss.loss)
          steps_per_sec = (global_step_val - timed_at_step) / time_acc
          logging.info('%.3f steps/sec', steps_per_sec)
          sess.run(
              steps_per_second_summary,
              feed_dict={steps_per_second_ph: steps_per_sec})
          timed_at_step = global_step_val
          time_acc = 0

        if global_step_val % eval_interval == 0 and finetune:
          average_across_tasks = 0
          for idx in range(train_tasks):
            metrics = metric_utils.compute_summaries(
                eval_metrics,
                eval_py_env[idx],
                eval_py_policy,
                num_episodes=num_eval_episodes,
                global_step=global_step_val,
                callback=eval_metrics_callback,
                log=True,
            )
            average_across_tasks += metrics["AverageReturn"] / train_tasks
            sess.run(eval_summary_flush_op)
          returnsCache.append((global_step_val, average_across_tasks))
        if global_step_val % train_checkpoint_interval == 0:
          train_checkpointer.save(global_step=global_step_val)

        if global_step_val % policy_checkpoint_interval == 0:
          policy_checkpointer.save(global_step=global_step_val)

        if global_step_val % rb_checkpoint_interval == 0:
          rb_checkpointer.save(global_step=global_step_val)

        if global_step_val % plot_interval == 0 and finetune:
          print("Plotting returns...") 
          steps, returns = zip(*returnsCache)
          if finetune:
            steps = [x - 2000000 for x in steps]
          plt.plot(steps, returns)
          plt.ylabel('Average Return')
          plt.xlabel('Step')
          plt.ylim()
          plt.savefig(FLAGS.root_dir + '/plots/sac' + env_name[:-3] + str(int(global_step_val/1000)) + 'k.png')
          print("Done plotting...")

def main(_):
  tf.compat.v1.enable_resource_variables()
  logging.set_verbosity(logging.INFO)
  gin.parse_config_files_and_bindings(FLAGS.gin_file, FLAGS.gin_param)
  train_eval(FLAGS.root_dir, FLAGS.finetune, FLAGS.direc)


if __name__ == '__main__':
  flags.mark_flag_as_required('root_dir')
  app.run(main)
