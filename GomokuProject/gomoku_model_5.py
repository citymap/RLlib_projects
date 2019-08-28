from __future__ import absolute_import, division, print_function, unicode_literals
import tensorflow as tf
import ray
from ray import tune
from ray.rllib.models import ModelCatalog
from ray.rllib.models.tf.tf_modelv2 import TFModelV2
from ray.rllib.models.tf.tf_action_dist import *
import numpy as np

BOARD_SIZE=10
NUM_IN_A_ROW=5

class GomokuModel(TFModelV2):
    def __init__(self, obs_space, action_space, num_outputs, model_config, name):
        super(GomokuModel, self).__init__(obs_space, action_space,num_outputs, model_config, name)
        if 'use_symmetry' in model_config['custom_options']:
            self.use_symmetry = model_config['custom_options']['use_symmetry']
        else:
            self.use_symmetry = False
        act_fun = lambda x: tf.nn.leaky_relu(x, alpha=0.05)
        regul=tf.keras.regularizers.l2(l=self.model_config['custom_options']['reg_loss'])
        input_shp = obs_space.original_space.spaces['real_obs']

        self.inputs = tf.keras.layers.Input(shape=input_shp.shape, name="observations")
        self.outputs = int(np.sqrt(num_outputs))

        bk_shape = tf.fill(tf.shape(self.inputs), 1.0)
        mrg_inp_bk = tf.concat([self.inputs, bk_shape], axis=3)

        layer_1 = tf.keras.layers.Conv2D(kernel_size=5, filters=128, padding='same',
                                         activation=act_fun, kernel_regularizer=regul)(mrg_inp_bk)
        layer_2 = tf.keras.layers.Conv2D(kernel_size=1, filters=64, padding='same',
                                         activation=act_fun, kernel_regularizer=regul)(layer_1)
        layer_3 = tf.keras.layers.Conv2D(kernel_size=3, filters=32, padding='same',
                                         activation=act_fun, kernel_regularizer=regul)(layer_2)
        layer_4 = tf.keras.layers.Conv2D(kernel_size=3, filters=16, padding='same',
                                         activation=act_fun, kernel_regularizer=regul)(layer_3)
        layer_5 = tf.keras.layers.Conv2D(kernel_size=3, filters=8, padding='same',
                                         activation=act_fun, kernel_regularizer=regul)(layer_4)
        layer_out = tf.keras.layers.Conv2D(kernel_size=3, filters=1, padding='same',
                                         activation=act_fun, kernel_regularizer=regul)(layer_5)
        value_out = tf.keras.layers.Dense(1, activation=None, kernel_regularizer=regul)(layer_5)
        self.base_model = tf.keras.Model(self.inputs, [layer_out, value_out])
        self.base_model.summary()
        self.register_variables(self.base_model.variables)

    def get_sym_output(self, board, rotc=0, is_flip=False):
        board_tmp = tf.identity(board)
        board_tmp = tf.image.rot90(board_tmp, k=rotc % 4)
        if is_flip:
            board_tmp = tf.image.flip_up_down(board_tmp)
        model_out, val_out = self.base_model(board_tmp)

        if is_flip:
            model_out = tf.image.flip_up_down(model_out)

        model_out = tf.image.rot90(model_out, k=(4-rotc) % 4)
        model_out_fin = tf.reshape(model_out, [-1, self.outputs**2])
        value_out_fin = tf.reshape(val_out, [-1])
        return model_out_fin, value_out_fin

    def forward(self, input_dict, state, seq_lens):
        board = input_dict["obs"]["real_obs"]
        if self.use_symmetry:
            model_rot_out = [None]*8
            value_out = [None]*8
            for j in range(2):
                for i in range(4):
                    model_rot_out[j*4+i], value_out[j*4+i] = self.get_sym_output(board, i, j)
            model_out = tf.math.add_n(model_rot_out)
            self.value_out = tf.math.add_n(value_out)
        else:
            model_out, self.value_out = self.base_model(board)
            model_out = tf.reshape(model_out, [-1, self.outputs ** 2])
            self.value_out = tf.reshape(self.value_out, [-1])

        inf_mask = tf.maximum(tf.math.log(input_dict["obs"]["action_mask"]), tf.float32.min)
        model_out = model_out+inf_mask
        return model_out, state

    def value_function(self):
        return self.value_out

    def policy_variables(self):
        """Return the list of variables for the policy net."""
        return list(self.action_net.variables)

def gen_policy(GENV):
    config = {
        "model": {
            "custom_model": 'GomokuModel',
            "custom_options": {"use_symmetry": True, "reg_loss": 0.001},
        },
        "custom_action_dist": Categorical,
    }
    return (None, GENV.observation_space, GENV.action_space, config)

def map_fn(agent_id):
        return "policy_0"

def clb_episode_end(info):
    episode = info["episode"]
    episode.custom_metrics["agent_0_win_rate"] = episode.last_info_for("agent_0")["result"]
    episode.custom_metrics["agent_1_win_rate"] = episode.last_info_for("agent_1")["result"]


def get_trainer(GENV):
    ModelCatalog.register_custom_model("GomokuModel", GomokuModel)
    trainer = ray.rllib.agents.a3c.A3CTrainer(env="GomokuEnv", config={
        "multiagent": {
            "policies": {"policy_0": gen_policy(GENV)},
            "policy_mapping_fn": map_fn,
            },
        "callbacks":
            {"on_episode_end": clb_episode_end},
    }, logger_creator=lambda _: ray.tune.logger.NoopLogger({}, None))
    return trainer