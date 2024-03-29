from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import gin
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from tf_agents.agents import tf_agent
#from tf_agents.policies import actor_policy
import latent_actor_policy 
from tf_agents.trajectories import trajectory
from tf_agents.utils import common
from tf_agents.utils import eager_utils
from tf_agents.utils import nest_utils
import latent_inference_network

EPS = 1e-20

SacLossInfo = collections.namedtuple(
    'SacLossInfo', ('critic_loss', 'actor_loss', 'alpha_loss'))


@gin.configurable
def std_clip_transform(stddevs):
  stddevs = tf.nest.map_structure(lambda t: tf.clip_by_value(t, -20, 2),
                                  stddevs)
  return tf.exp(stddevs)


@gin.configurable
class SacAgent(tf_agent.TFAgent):
  """A SAC Agent."""

  def __init__(self,
               time_step_spec,
               action_spec,
               finetune,
               critic_network,
               actor_network,
               action_generator,
               actor_optimizer,
               critic_optimizer,
               alpha_optimizer,
               actor_policy_ctor=latent_actor_policy.ActorPolicy,
               critic_network_2=None,
               target_critic_network=None,
               target_critic_network_2=None,
               target_update_tau=1.0,
               target_update_period=1,
               td_errors_loss_fn=tf.math.squared_difference,
               gamma=1.0,
               reward_scale_factor=1.0,
               initial_log_alpha=0.0,
               target_entropy=None,
               gradient_clipping=None,
               debug_summaries=False,
               summarize_grads_and_vars=False,
               train_step_counter=None,
               name=None):
    """Creates a SAC Agent.
    Args:
      time_step_spec: A `TimeStep` spec of the expected time_steps.
      action_spec: A nest of BoundedTensorSpec representing the actions.
      critic_network: A function critic_network((observations, actions)) that
        returns the q_values for each observation and action.
      actor_network: A function actor_network(observation, action_spec) that
        returns action distribution.
      actor_optimizer: The optimizer to use for the actor network.
      critic_optimizer: The default optimizer to use for the critic network.
      alpha_optimizer: The default optimizer to use for the alpha variable.
      actor_policy_ctor: The policy class to use.
      critic_network_2: (Optional.)  A `tf_agents.network.Network` to be used as
        the second critic network during Q learning.  The weights from
        `critic_network` are copied if this is not provided.
      target_critic_network: (Optional.)  A `tf_agents.network.Network` to be
        used as the target critic network during Q learning. Every
        `target_update_period` train steps, the weights from `critic_network`
        are copied (possibly withsmoothing via `target_update_tau`) to `
        target_critic_network`.  If `target_critic_network` is not provided, it
        is created by making a copy of `critic_network`, which initializes a new
        network with the same structure and its own layers and weights.
        Performing a `Network.copy` does not work when the network instance
        already has trainable parameters (e.g., has already been built, or when
        the network is sharing layers with another).  In these cases, it is up
        to you to build a copy having weights that are not shared with the
        original `critic_network`, so that this can be used as a target network.
        If you provide a `target_critic_network` that shares any weights with
        `critic_network`, a warning will be logged but no exception is thrown.
      target_critic_network_2: (Optional.) Similar network as
        target_critic_network but for the critic_network_2. See documentation
        for target_critic_network. Will only be used if 'critic_network_2' is
        also specified.
      target_update_tau: Factor for soft update of the target networks.
      target_update_period: Period for soft update of the target networks.
      td_errors_loss_fn:  A function for computing the elementwise TD errors
        loss.
      gamma: A discount factor for future rewards.
      reward_scale_factor: Multiplicative scale for the reward.
      initial_log_alpha: Initial value for log_alpha.
      target_entropy: The target average policy entropy, for updating alpha. The
        default value is negative of the total number of actions.
      gradient_clipping: Norm length to clip gradients.
      debug_summaries: A bool to gather debug summaries.
      summarize_grads_and_vars: If True, gradient and network variable summaries
        will be written during training.
      train_step_counter: An optional counter to increment every time the train
        op is run.  Defaults to the global_step.
      name: The name of this agent. All variables in this module will fall under
        that name. Defaults to the class name.
    """
    tf.Module.__init__(self, name=name)
    print("Initializing SAC Agent...")
    flat_action_spec = tf.nest.flatten(action_spec)
    for spec in flat_action_spec:
      if spec.dtype.is_integer:
        raise NotImplementedError(
            'SacAgent does not currently support discrete actions. '
            'Action spec: {}'.format(action_spec))

    self._critic_network_1 = critic_network
    self._critic_network_1.create_variables()
    if target_critic_network:
      target_critic_network.create_variables()
    self._target_critic_network_1 = (
        common.maybe_copy_target_network_with_checks(self._critic_network_1,
                                                     target_critic_network,
                                                     'TargetCriticNetwork1'))

    if critic_network_2 is not None:
      self._critic_network_2 = critic_network_2
    else:
      self._critic_network_2 = critic_network.copy(name='CriticNetwork2')
      # Do not use target_critic_network_2 if critic_network_2 is None.
      target_critic_network_2 = None
    self._critic_network_2.create_variables()
    if target_critic_network_2:
      target_critic_network_2.create_variables()
    self._target_critic_network_2 = (
        common.maybe_copy_target_network_with_checks(self._critic_network_2,
                                                     target_critic_network_2,
                                                     'TargetCriticNetwork2'))
    print("Creating actor network variables")
    if actor_network:
      actor_network.create_variables()
    self._actor_network = actor_network
    print("initializing policy in agent intialization")
    policy = actor_policy_ctor(
        time_step_spec=time_step_spec,
        action_spec=action_spec,
        actor_network=self._actor_network,
        training=False)
    print("Initializing train policy in agent init")
    self._train_policy = actor_policy_ctor(
        time_step_spec=time_step_spec,
        action_spec=action_spec,
        actor_network=self._actor_network,
        training=True)

    self._log_alpha = common.create_variable(
        'initial_log_alpha',
        initial_value=initial_log_alpha,
        dtype=tf.float32,
        trainable=True)

    # If target_entropy was not passed, set it to negative of the total number
    # of action dimensions.
    if target_entropy is None:
      flat_action_spec = tf.nest.flatten(action_spec)
      target_entropy = -np.sum([
          np.product(single_spec.shape.as_list())
          for single_spec in flat_action_spec
      ])

    self._finetune = finetune
    self._target_update_tau = target_update_tau
    self._target_update_period = target_update_period
    self._actor_optimizer = actor_optimizer
    self._critic_optimizer = critic_optimizer
    self._alpha_optimizer = alpha_optimizer
    self._td_errors_loss_fn = td_errors_loss_fn
    self._gamma = gamma
    self._reward_scale_factor = reward_scale_factor
    self._target_entropy = target_entropy
    self._gradient_clipping = gradient_clipping
    self._debug_summaries = debug_summaries
    self._summarize_grads_and_vars = summarize_grads_and_vars
    self._update_target = self._get_target_updater(
        tau=self._target_update_tau, period=self._target_update_period)
    self._action_generator = action_generator
    
    z_inference_network_ctor = latent_inference_network.ZInferenceNetwork
    self._z_inference_network = z_inference_network_ctor(input_tensor_spec=(time_step_spec.observation, action_spec))
    self._z_inference_network.create_variables() 
    train_sequence_length = 2 if not critic_network.state_spec else None

    super(SacAgent, self).__init__(
        time_step_spec,
        action_spec,
        policy=policy,
        collect_policy=policy,
        train_sequence_length=train_sequence_length,
        debug_summaries=debug_summaries,
        summarize_grads_and_vars=summarize_grads_and_vars,
        train_step_counter=train_step_counter)

  def _initialize(self):
    """Returns an op to initialize the agent.
    Copies weights from the Q networks to the target Q network.
    """
    common.soft_variables_update(
        self._critic_network_1.variables,
        self._target_critic_network_1.variables,
        tau=1.0)
    common.soft_variables_update(
        self._critic_network_2.variables,
        self._target_critic_network_2.variables,
        tau=1.0)

  def _experience_to_transitions(self, experience):
    transitions = trajectory.to_transition(experience)
    time_steps, policy_steps, next_time_steps = transitions
    actions = policy_steps.action
    if (self.train_sequence_length is not None and
        self.train_sequence_length == 2):
      # Sequence empty time dimension if critic network is stateless.
      time_steps, actions, next_time_steps = tf.nest.map_structure(
          lambda t: tf.squeeze(t, axis=1),
          (time_steps, actions, next_time_steps))
    return time_steps, actions, next_time_steps

  def _train(self, experience, weights):
    """Returns a train op to update the agent's networks.
    This method trains with the provided batched experience.
    Args:
      experience: A time-stacked trajectory object.
      weights: Optional scalar or elementwise (per-batch-entry) importance
        weights.
    Returns:
      A train_op.
    Raises:
      ValueError: If optimizers are None and no default value was provided to
        the constructor.
    """
    print("Running train forward pass")
    time_steps, actions, next_time_steps = self._experience_to_transitions(
        experience)
    trainable_critic_variables = (
        self._critic_network_1.trainable_variables +
        self._critic_network_2.trainable_variables)
    with tf.GradientTape(watch_accessed_variables=False) as tape:
      assert trainable_critic_variables, ('No trainable critic variables to '
                                          'optimize.')
      tape.watch(trainable_critic_variables)
      critic_loss = self.critic_loss(
          time_steps,
          actions,
          next_time_steps,
          td_errors_loss_fn=self._td_errors_loss_fn,
          gamma=self._gamma,
          reward_scale_factor=self._reward_scale_factor,
          weights=weights)

    tf.debugging.check_numerics(critic_loss, 'Critic loss is inf or nan.')
    critic_grads = tape.gradient(critic_loss, trainable_critic_variables)
    self._apply_gradients(critic_grads, trainable_critic_variables,
                          self._critic_optimizer)

    trainable_actor_variables = self._actor_network.trainable_variables
    trainable_actor_variables = [var for var in self._actor_network.trainable_variables if var not in self._action_generator.trainable_variables]
    with tf.GradientTape(watch_accessed_variables=False) as tape:
      assert trainable_actor_variables, ('No trainable actor variables to '
                                         'optimize.')
      tape.watch(trainable_actor_variables)
      print("Computing Actor loss")
      actor_loss = self.actor_loss(time_steps, weights=weights)
    tf.debugging.check_numerics(actor_loss, 'Actor loss is inf or nan.')
    actor_grads = tape.gradient(actor_loss, trainable_actor_variables)
    self._apply_gradients(actor_grads, trainable_actor_variables,
                          self._actor_optimizer)

    alpha_variable = [self._log_alpha]
    with tf.GradientTape(watch_accessed_variables=False) as tape:
      assert alpha_variable, 'No alpha variable to optimize.'
      tape.watch(alpha_variable)
      alpha_loss = self.alpha_loss(time_steps, weights=weights)
    tf.debugging.check_numerics(alpha_loss, 'Alpha loss is inf or nan.')
    alpha_grads = tape.gradient(alpha_loss, alpha_variable)
    self._apply_gradients(alpha_grads, alpha_variable, self._alpha_optimizer)
   
    if not self._finetune: 
      vae_variables = (self._z_inference_network.trainable_variables + self._action_generator.trainable_variables)
      with tf.GradientTape() as tape:
        assert vae_variables, ('No trainable vae variables to '
                                           'optimize.')
        tape.watch(vae_variables)
        print("Computing VAE Loss")
        vae_loss = self.vae_loss(time_steps, actions, next_time_steps)
      tf.debugging.check_numerics(vae_loss, 'VAE loss is inf or nan.')
      vae_grads = tape.gradient(vae_loss, vae_variables)
      self._apply_gradients(vae_grads, vae_variables, self._actor_optimizer) 

    with tf.name_scope('Losses'):
      tf.compat.v2.summary.scalar(
          name='critic_loss', data=critic_loss, step=self.train_step_counter)
      tf.compat.v2.summary.scalar(
          name='actor_loss', data=actor_loss, step=self.train_step_counter)
      tf.compat.v2.summary.scalar(
          name='alpha_loss', data=alpha_loss, step=self.train_step_counter)

    self.train_step_counter.assign_add(1)
    self._update_target()
    if not self._finetune:
      total_loss = critic_loss + actor_loss + alpha_loss + tf.dtypes.cast(vae_loss, 'float32')
    else:
      total_loss = critic_loss + actor_loss + alpha_loss

    extra = SacLossInfo(critic_loss=critic_loss,
                        actor_loss=actor_loss,
                        alpha_loss=alpha_loss)

    return tf_agent.LossInfo(loss=total_loss, extra=extra)

  def _apply_gradients(self, gradients, variables, optimizer):
    # list(...) is required for Python3.
    grads_and_vars = list(zip(gradients, variables))
    if self._gradient_clipping is not None:
      grads_and_vars = eager_utils.clip_gradient_norms(grads_and_vars,
                                                       self._gradient_clipping)

    if self._summarize_grads_and_vars:
      eager_utils.add_variables_summaries(grads_and_vars,
                                          self.train_step_counter)
      eager_utils.add_gradients_summaries(grads_and_vars,
                                          self.train_step_counter)

    optimizer.apply_gradients(grads_and_vars)

  def _get_target_updater(self, tau=1.0, period=1):
    """Performs a soft update of the target network parameters.
    For each weight w_s in the original network, and its corresponding
    weight w_t in the target network, a soft update is:
    w_t = (1- tau) x w_t + tau x ws
    Args:
      tau: A float scalar in [0, 1]. Default `tau=1.0` means hard update.
      period: Step interval at which the target network is updated.
    Returns:
      A callable that performs a soft update of the target network parameters.
    """
    with tf.name_scope('update_target'):

      def update():
        """Update target network."""
        critic_update_1 = common.soft_variables_update(
            self._critic_network_1.variables,
            self._target_critic_network_1.variables,
            tau,
            tau_non_trainable=1.0)
        critic_update_2 = common.soft_variables_update(
            self._critic_network_2.variables,
            self._target_critic_network_2.variables,
            tau,
            tau_non_trainable=1.0)
        return tf.group(critic_update_1, critic_update_2)

      return common.Periodically(update, period, 'update_targets')

  def _actions_and_log_probs(self, time_steps):
    """Get actions and corresponding log probabilities from policy."""
    # Get raw action distribution from policy, and initialize bijectors list.
    batch_size = nest_utils.get_outer_shape(time_steps, self._time_step_spec)[0]
    policy_state = self._train_policy.get_initial_state(batch_size)
    action_distribution = self._train_policy.distribution(
        time_steps, policy_state=policy_state).action

    # Sample actions and log_pis from transformed distribution.
    actions = tf.nest.map_structure(lambda d: d.sample(), action_distribution)
    log_pi = common.log_probability(action_distribution, actions,
                                    self.action_spec)

    return actions, log_pi

  def critic_loss(self,
                  time_steps,
                  actions,
                  next_time_steps,
                  td_errors_loss_fn,
                  gamma=1.0,
                  reward_scale_factor=1.0,
                  weights=None):
    """Computes the critic loss for SAC training.
    Args:
      time_steps: A batch of timesteps.
      actions: A batch of actions.
      next_time_steps: A batch of next timesteps.
      td_errors_loss_fn: A function(td_targets, predictions) to compute
        elementwise (per-batch-entry) loss.
      gamma: Discount for future rewards.
      reward_scale_factor: Multiplicative factor to scale rewards.
      weights: Optional scalar or elementwise (per-batch-entry) importance
        weights.
    Returns:
      critic_loss: A scalar critic loss.
    """
    with tf.name_scope('critic_loss'):
      tf.nest.assert_same_structure(actions, self.action_spec)
      tf.nest.assert_same_structure(time_steps, self.time_step_spec)
      tf.nest.assert_same_structure(next_time_steps, self.time_step_spec)

      next_actions, next_log_pis = self._actions_and_log_probs(next_time_steps)
      target_input = (next_time_steps.observation, next_actions)
      target_q_values1, unused_network_state1 = self._target_critic_network_1(
          target_input, next_time_steps.step_type, training=False)
      target_q_values2, unused_network_state2 = self._target_critic_network_2(
          target_input, next_time_steps.step_type, training=False)
      target_q_values = (
          tf.minimum(target_q_values1, target_q_values2) -
          tf.exp(self._log_alpha) * next_log_pis)

      td_targets = tf.stop_gradient(
          reward_scale_factor * next_time_steps.reward +
          gamma * next_time_steps.discount * target_q_values)

      pred_input = (time_steps.observation, actions)
      pred_td_targets1, _ = self._critic_network_1(
          pred_input, time_steps.step_type, training=True)
      pred_td_targets2, _ = self._critic_network_2(
          pred_input, time_steps.step_type, training=True)
      critic_loss1 = td_errors_loss_fn(td_targets, pred_td_targets1)
      critic_loss2 = td_errors_loss_fn(td_targets, pred_td_targets2)
      critic_loss = critic_loss1 + critic_loss2

      if weights is not None:
        critic_loss *= weights

      if nest_utils.is_batched_nested_tensors(
          time_steps, self.time_step_spec, num_outer_dims=2):
        # Sum over the time dimension.
        critic_loss = tf.reduce_sum(input_tensor=critic_loss, axis=1)

      # Take the mean across the batch.
      critic_loss = tf.reduce_mean(input_tensor=critic_loss)

      if self._debug_summaries:
        td_errors1 = td_targets - pred_td_targets1
        td_errors2 = td_targets - pred_td_targets2
        td_errors = tf.concat([td_errors1, td_errors2], axis=0)
        common.generate_tensor_summaries('td_errors', td_errors,
                                         self.train_step_counter)
        common.generate_tensor_summaries('td_targets', td_targets,
                                         self.train_step_counter)
        common.generate_tensor_summaries('pred_td_targets1', pred_td_targets1,
                                         self.train_step_counter)
        common.generate_tensor_summaries('pred_td_targets2', pred_td_targets2,
                                         self.train_step_counter)

      return critic_loss

  def actor_loss(self, time_steps, weights=None):
    """Computes the actor_loss for SAC training.
    Args:
      time_steps: A batch of timesteps.
      weights: Optional scalar or elementwise (per-batch-entry) importance
        weights.
    Returns:
      actor_loss: A scalar actor loss.
    """
    with tf.name_scope('actor_loss'):
      tf.nest.assert_same_structure(time_steps, self.time_step_spec)

      actions, log_pi = self._actions_and_log_probs(time_steps)
      target_input = (time_steps.observation, actions)
      target_q_values1, _ = self._critic_network_1(target_input,
                                                   time_steps.step_type,
                                                   training=False)
      target_q_values2, _ = self._critic_network_2(target_input,
                                                   time_steps.step_type,
                                                   training=False)
      target_q_values = tf.minimum(target_q_values1, target_q_values2)
      actor_loss = tf.exp(self._log_alpha) * log_pi - target_q_values
      if nest_utils.is_batched_nested_tensors(
          time_steps, self.time_step_spec, num_outer_dims=2):
        # Sum over the time dimension.
        actor_loss = tf.reduce_sum(input_tensor=actor_loss, axis=1)
      if weights is not None:
        actor_loss *= weights
      actor_loss = tf.reduce_mean(input_tensor=actor_loss)

      if self._debug_summaries:
        common.generate_tensor_summaries('actor_loss', actor_loss,
                                         self.train_step_counter)
        common.generate_tensor_summaries('actions', actions,
                                         self.train_step_counter)
        common.generate_tensor_summaries('log_pi', log_pi,
                                         self.train_step_counter)
        tf.compat.v2.summary.scalar(
            name='entropy_avg',
            data=-tf.reduce_mean(input_tensor=log_pi),
            step=self.train_step_counter)
        common.generate_tensor_summaries('target_q_values', target_q_values,
                                         self.train_step_counter)
        batch_size = nest_utils.get_outer_shape(
            time_steps, self._time_step_spec)[0]
        policy_state = self._train_policy.get_initial_state(batch_size)
        action_distribution = self._train_policy.distribution(
            time_steps, policy_state).action
        if isinstance(action_distribution, tfp.distributions.Normal):
          common.generate_tensor_summaries('act_mean', action_distribution.loc,
                                           self.train_step_counter)
          common.generate_tensor_summaries(
              'act_stddev', action_distribution.scale, self.train_step_counter)
        elif isinstance(action_distribution, tfp.distributions.Categorical):
          common.generate_tensor_summaries(
              'act_mode', action_distribution.mode(), self.train_step_counter)
        try:
          common.generate_tensor_summaries('entropy_action',
                                           action_distribution.entropy(),
                                           self.train_step_counter)
        except NotImplementedError:
          pass  # Some distributions do not have an analytic entropy.

      return actor_loss

  def alpha_loss(self, time_steps, weights=None):
    """Computes the alpha_loss for EC-SAC training.
    Args:
      time_steps: A batch of timesteps.
      weights: Optional scalar or elementwise (per-batch-entry) importance
        weights.
    Returns:
      alpha_loss: A scalar alpha loss.
    """
    with tf.name_scope('alpha_loss'):
      tf.nest.assert_same_structure(time_steps, self.time_step_spec)

      unused_actions, log_pi = self._actions_and_log_probs(time_steps)
      entropy_diff = tf.stop_gradient(-log_pi - self._target_entropy)
      alpha_loss = (self._log_alpha * entropy_diff)

      if nest_utils.is_batched_nested_tensors(
          time_steps, self.time_step_spec, num_outer_dims=2):
        # Sum over the time dimension.
        alpha_loss = tf.reduce_sum(input_tensor=alpha_loss, axis=1)

      if weights is not None:
        alpha_loss *= weights

      alpha_loss = tf.reduce_mean(input_tensor=alpha_loss)

      if self._debug_summaries:
        common.generate_tensor_summaries('alpha_loss', alpha_loss,
                                         self.train_step_counter)
        common.generate_tensor_summaries('entropy_diff', entropy_diff,
                                         self.train_step_counter)

        tf.compat.v2.summary.scalar(
            name='log_alpha',
            data=self._log_alpha,
            step=self.train_step_counter)

      return alpha_loss
  
  def vae_loss(self, time_steps, actions, next_time_steps):
    
    def _sample_gaussian_noise(means, stddevs):
      return means + stddevs * tf.random_normal(
            tf.shape(stddevs), 0., 1., dtype=tf.float64)
    
    def log_normal(x, mean, stddev):
      stddev = tf.abs(stddev)
      stddev = tf.add(stddev, EPS) 
      return -0.5 * tf.reduce_sum((tf.dtypes.cast(tf.log(2 * np.pi), 'float64') + tf.dtypes.cast(tf.log(tf.square(stddev)), 'float64')) + tf.dtypes.cast(tf.square(x-mean), 'float64') / tf.dtypes.cast(tf.square(stddev), 'float64'), axis=-1)
      
      '''
      return -0.5 * tf.reduce_sum(
        (tf.log(2 * np.pi) + tf.log(tf.square(stddev))
         + tf.square(x - mean) / tf.square(stddev)),
         axis=-1)
      '''
    def _normal_kld(z, z_mean, z_stddev, weights=1.0):
      kld_array = (log_normal(z, z_mean, z_stddev) -
                      log_normal(z, 0.0, 1.0))
      return tf.losses.compute_weighted_loss(kld_array, weights)  
    
    def l2_loss(targets,
            outputs,
            weights=1.0,
            reduction=tf.losses.Reduction.SUM_BY_NONZERO_WEIGHTS):
      loss = 0.5 * tf.reduce_sum(tf.dtypes.cast(tf.square(tf.dtypes.cast(targets, 'float64') - tf.dtypes.cast(outputs, 'float64')), 'float64'), axis=-1)
      return tf.losses.compute_weighted_loss(loss, weights, reduction=reduction)    
    def action_loss(targets, outputs, weights=1.0):
      assert len(targets.shape) == len(outputs.shape)
      # Weight starting position by 10.
      return 10.0 * l2_loss(
        targets=targets[..., :2],
        outputs=outputs[..., :2],
        weights=weights) + l2_loss(
            targets=targets[..., 2:],
            outputs=outputs[..., 2:],
            weights=weights)

    with tf.name_scope('loss_vae'):
        
      z_means, z_stddevs = self._z_inference_network(
       	(time_steps.observation, actions))
      zs = _sample_gaussian_noise(z_means, z_stddevs)
      # Predict
      pred_actions = self._action_generator(
	(time_steps.observation, zs))

      # Losses
      z_kld = _normal_kld(
	zs,
	z_means,
	z_stddevs)

      action_loss = action_loss(
	    actions,
	    pred_actions) 

      # Summaries.
      tf.compat.v2.summary.scalar(
	name='z_kld', data=z_kld,
	step=self.train_step_counter)
      tf.compat.v2.summary.scalar(
	name='action_loss', data=action_loss,
	step=self.train_step_counter)

    return z_kld + action_loss
