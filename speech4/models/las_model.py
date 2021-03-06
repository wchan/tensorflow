import math
import numpy as np
import os.path
import time

import tensorflow as tf
from tensorflow.core.framework import speech4_pb2
from tensorflow.python.ops import attention_mask_ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import clip_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import gradients
from tensorflow.python.ops import gru_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import rnn
from tensorflow.python.ops import rnn_cell
from tensorflow.python.ops import seq2seq
from tensorflow.python.ops import sparse_ops
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops.gen_user_ops import s4_parse_utterance
from tensorflow.python.platform import gfile


class LASModel(object):
  def __init__(self, sess, dataset, logdir, ckpt, forward_only, batch_size,
      model_params, optimization_params=None, visualization_params=None, dataset_size=None):
    self.dataset = dataset
    self.dataset_size = dataset_size
    self.logdir = logdir

    self.batch_size = batch_size

    self.model_params = model_params
    if not self.model_params.encoder_prefix:
      self.model_params.encoder_prefix = "encoder"
    if not self.model_params.encoder_embedding:
      self.model_params.encoder_embedding = "encoder_embedding"
    if not self.model_params.decoder_prefix:
      self.model_params.decoder_prefix = "decoder"

    if self.model_params.frame_stack == 0:
      self.model_params.frame_stack = 1
    if self.model_params.frame_skip == 0:
      self.model_params.frame_skip = 1

    self.optimization_params = optimization_params
    self.visualization_params = visualization_params
    if forward_only and self.optimization_params:
      self.optimization_params.sample_prob = 0.0

    self.step_total = 0
    self.step_time_total = 0
    self.global_epochs = 0

    self.epoch_accuracy = 0.0

    self.global_step = tf.Variable(0, trainable=False)

    # Create the inputs.
    self.create_input_layer(forward_only)

    # Create the encoder-encoder.
    self.create_encoder()
    if not self.model_params.encoder_only:
      self.create_decoder()

    # Create the loss.
    if not self.model_params.input_layer:
      self.create_loss()

    if not forward_only:
      # Create the optimizer.
      self.create_optimizer()

    variables = tf.all_variables()
    sess.run(tf.initialize_all_variables())

    if optimization_params:
      if optimization_params.adam.reset:
        filtered_variables = []
        for v in variables:
          if 'Adam' not in v.name and 'beta1_power' not in v.name and 'beta2_power' not in v.name:
            filtered_variables.append(v)
        variables = filtered_variables
      if optimization_params.adagrad.reset:
        filtered_variables = []
        for v in variables:
          if 'Adagrad' not in v.name:
            filtered_variables.append(v)
        variables = filtered_variables
      if optimization_params.adadelta.reset:
        filtered_variables = []
        for v in variables:
          if 'Adadelta' not in v.name:
            filtered_variables.append(v)
        variables = filtered_variables
      filtered_variables = []
      for v in variables:
        if 'Pinyin' not in v.name:
          filtered_variables.append(v)
      variables = filtered_variables
      #filtered_variables = []
      #for v in variables:
      #  if "encoder_0" in v.name or "encoder_1" in v.name or "encoder_2" in v.name or "encoder_3" in v.name:
      #    print v.name
      #    filtered_variables.append(v)
      #variables = filtered_variables
    for v in variables:
      print "loading: %s %s" % (v.name, str(v.get_shape()))
    self.saver = tf.train.Saver(variables)

    if gfile.Exists(ckpt):
      print("Reading model parameters from %s" % ckpt)
      self.saver.restore(sess, ckpt)
    self.saver = tf.train.Saver(tf.all_variables())

  def create_input_layer(self, forward_only):
    self.features = []
    self.tokens = []
    self.tokens_weights = []
    if self.model_params.input_layer == 'placeholder':
      for idx in range(self.model_params.features_len_max):
        self.features.append(tf.placeholder(
          tf.float32, shape=(self.batch_size, self.model_params.features_width * self.model_params.frame_stack),
          name="features_%d" % idx))
      for idx in range(self.model_params.tokens_len_max + 1):
        self.tokens.append(tf.placeholder(
            tf.int32, shape=(self.batch_size), name="tokens_%d" % idx))
        self.tokens_weights.append(tf.placeholder(
            tf.float32, shape=(self.batch_size), name="tokens_weights_%d" % idx))

      self.features_len = tf.placeholder(
          tf.int64, shape=(self.batch_size), name="features_len")
      self.tokens_len = tf.placeholder(
          tf.int64, shape=(self.batch_size), name="tokens_len")
    elif self.model_params.input_layer == "decoder":
      for idx in range(self.model_params.features_len_max):
        self.features.append(tf.placeholder(
          tf.float32, shape=(self.batch_size, self.model_params.features_width * self.model_params.frame_stack),
          name="features_%d" % idx))
      self.features_len = tf.placeholder(
          tf.int64, shape=(self.batch_size), name="features_len")

      # Only the first token is given.
      self.tokens.append(tf.placeholder(
          tf.int32, shape=(self.batch_size), name="tokens_0"))
      self.tokens_weights.append(tf.placeholder(
          tf.float32, shape=(self.batch_size), name="tokens_weights_0"))
      # Everything else must come from the graph itself.
      for idx in range(self.model_params.tokens_len_max):
        self.tokens.append(None)
        self.tokens_weights.append(None)

      self.tokens_len = tf.placeholder(
          tf.int64, shape=(self.batch_size), name="tokens_len")
    else:
      print("Dataset: %s" % self.dataset)
      assert os.path.isfile(self.dataset)
      filename_queue = tf.train.string_input_producer([self.dataset])

      reader = tf.TFRecordReader()
      _, serialized = reader.read(filename_queue)

      if forward_only or self.optimization_params.shuffle == False:
        serialized = tf.train.batch(
            [serialized], batch_size=self.batch_size, num_threads=2,
            capacity=self.batch_size * 4 + 512)
      else:
        serialized = tf.train.shuffle_batch(
            [serialized], batch_size=self.batch_size, num_threads=2,
            capacity=self.batch_size * 4 + 512, min_after_dequeue=512, seed=self.global_epochs)

      # Parse the batched of serialized strings into the relevant utterance features.
      self.features, self.features_fbank, self.features_len, _, self.features_weight, self.text, self.tokens, self.tokens_pinyin, self.tokens_len, self.tokens_weights, self.tokens_pinyin_weights, self.uttid = s4_parse_utterance(
          serialized, features_len_max=self.model_params.features_len_max,
          tokens_len_max=self.model_params.tokens_len_max + 1,
          frame_stack=self.model_params.frame_stack,
          frame_skip=self.model_params.frame_skip)
      assert len(self.tokens) == len(self.tokens_weights)
      if self.model_params.pinyin_ext:
        assert len(self.tokens) == len(self.tokens_pinyin)
        assert len(self.tokens) == len(self.tokens_pinyin_weights)
      for feature_fbank in self.features_fbank:
        feature_fbank.set_shape([self.batch_size, 40])

    # Add the shape to the features.
    for feature in self.features:
      feature.set_shape([self.batch_size, self.model_params.features_width * self.model_params.frame_stack])
    for token in self.tokens:
      if token:
        token.set_shape([self.batch_size])

  def create_encoder(self):
    start_time = time.time()

    self.encoder_states = [[self.features, self.features_len]]

    if self.model_params.encoder_layer:
      for s in self.model_params.encoder_layer:
        self.create_encoder_layer(subsample_input=int(s))
    else:
      self.create_encoder_layer()
      self.create_encoder_layer()
      self.create_encoder_layer(subsample_input=2)
      self.create_encoder_layer(subsample_input=2)

    print('create_encoder graph time %f' % (time.time() - start_time))

  def create_encoder_layer(self, subsample_input=1, use_monolithic=True):
    with vs.variable_scope('%s_%d' % (self.model_params.encoder_prefix, len(self.encoder_states))):
      sequence_len_factor = tf.constant(
          subsample_input, shape=[self.batch_size], dtype=tf.int64)
      sequence_len = tf.div(self.encoder_states[-1][1], sequence_len_factor)
      if use_monolithic:
        # xs = self.encoder_states[-1][0]
        # if subsample_input > 1:
        #   xs = [xs[i:i + subsample_input] for i in range(0, len(xs), subsample_input)]
        #   xs = [array_ops.concat(1, x) for x in xs]
        xs = self.encoder_states[-1][0][0::subsample_input]
        self.encoder_states.append([gru_ops.gru(
            cell_size=self.model_params.encoder_cell_size,
            sequence_len=sequence_len, xs=xs)[-1], sequence_len])
      else:
        self.encoder_states.append([rnn.rnn(
            rnn_cell.GRUCell(self.model_params.encoder_cell_size),
            self.encoder_states[-1][0][0::subsample_input], dtype=tf.float32,
            sequence_length=sequence_len)[0], sequence_len])

      for encoder_state in self.encoder_states[-1][0]:
        encoder_state.set_shape([
            self.batch_size, self.model_params.encoder_cell_size])

  def create_decoder(self):
    start_time = time.time()

    self.decoder_states = []
    self.decoder_states_initial = []
    self.decoder_attentions_initial = []
    self.decoder_alignments_initial = []
    self.decoder_states.append(self.tokens[:-1])

    attention_states = []
    if len(self.encoder_states) > 1:
      attention_states = [
          array_ops.reshape(
              e, [self.batch_size, 1, self.model_params.encoder_cell_size], name='reshape_%d' % idx)
          for idx, e in enumerate(self.encoder_states[-1][0])]
      attention_states = array_ops.concat(1, attention_states)

    batch_size = self.batch_size
    attn_length = attention_states.get_shape()[1].value
    attn_size = attention_states.get_shape()[2].value

    encoder_states = array_ops.reshape(
        attention_states, [batch_size, attn_length, 1, attn_size])
    with vs.variable_scope(self.model_params.encoder_embedding):
      k = vs.get_variable("W", [1, 1, attn_size, self.model_params.attention_embedding_size])
    encoder_embedding = nn_ops.conv2d(encoder_states, k, [1, 1, 1, 1], "SAME")
    #encoder_embedding = tf.nn.relu(encoder_embedding)

    #self.create_decoder_layer(attention_states=attention_states)
    self.create_decoder_sequence(
        attention_states=attention_states, encoder_states=encoder_states,
        encoder_embedding=encoder_embedding)

    print('create_decoder graph time %f' % (time.time() - start_time))

  def create_decoder_sequence(
      self, attention_states, encoder_states, encoder_embedding, scope=None):
    with vs.variable_scope(self.model_params.decoder_prefix or scope):
      self.decoder_states = []
      self.logits_pinyin = []
      self.prob = []
      self.logprob = []
      states = []
      attentions = []
      alignments = None
      prev_logit = None
      for decoder_time_idx in range(len(self.tokens) - 1):
        if decoder_time_idx > 0:
          vs.get_variable_scope().reuse_variables()

        # RNN-Attention Decoder.
        (outputs, states, attentions, alignments) = self.create_decoder_cell(
            decoder_time_idx, states, attentions, alignments, encoder_states,
            encoder_embedding, prev_logit)
 
        # Logit.
        with vs.variable_scope("Logit"):
          logit = nn_ops.xw_plus_b(
              outputs[-1],
              vs.get_variable("Matrix", [outputs[-1].get_shape()[1].value, self.model_params.vocab_size]),
              vs.get_variable("Bias", [self.model_params.vocab_size]), name="Logit_%d" % decoder_time_idx)
          prev_logit = logit
          self.decoder_states.append(logit)
          prob = tf.nn.softmax(logit, name="Softmax_%d" % decoder_time_idx)
          self.prob.append(prob)
          self.logprob.append(tf.log(prob, name="LogProb_%d" % decoder_time_idx))

        # Pinyin Logits.
        if self.model_params.pinyin_ext:
          assert self.model_params.pinyin_dim == 7
          with vs.variable_scope("LogitPinyin"):
            pinyin_logit = nn_ops.xw_plus_b(
                outputs[-1],
                vs.get_variable("Matrix", [outputs[-1].get_shape()[1].value, self.model_params.pinyin_vocab_size * 7]),
                vs.get_variable("Bias", [self.model_params.pinyin_vocab_size * 7]), name="Logit_%d" % decoder_time_idx)
            pinyin_logit = array_ops.reshape(pinyin_logit, [self.batch_size * 7, self.model_params.pinyin_vocab_size])
            self.logits_pinyin.append(pinyin_logit)

      self.decoder_state_last = states[1:]
      self.decoder_alignment_last = filter(None, alignments)
      self.decoder_attention_last = filter(None, attentions)

  def create_decoder_cell(
      self, decoder_time_idx, states, attentions, prev_alignments, encoder_states,
      encoder_embedding, prev_logit, scope=None):
    batch_size = self.batch_size
    attention_embedding_size = self.model_params.attention_embedding_size
    decoder_cell_size = self.model_params.decoder_cell_size

    # Create the embedding layer.
    with tf.device("/cpu:0"):
      sqrt3 = np.sqrt(3)
      embedding = vs.get_variable(
          "embedding", [self.model_params.vocab_size, self.model_params.embedding_size],
          initializer=tf.random_uniform_initializer(-sqrt3, sqrt3))

      if decoder_time_idx == 0 or self.model_params.input_layer == 'placeholder':
        emb = embedding_ops.embedding_lookup(
            embedding, self.tokens[decoder_time_idx])
      elif self.model_params.input_layer == "decoder":
        # We want the arg max of the previous token here.
        assert prev_logit
        prev_token = math_ops.argmax(prev_logit, 1)
        emb = embedding_ops.embedding_lookup(embedding, prev_token)
      else:
        emb = embedding_ops.embedding_lookup(embedding, gru_ops.token_sample(
            self.tokens[decoder_time_idx], self.prob[-1],
            sample_prob=self.optimization_params.sample_prob,
            seed=len(self.prob)))
      emb.set_shape([batch_size, self.model_params.embedding_size])

    def create_attention(decoder_state, prev_alignment):
      with vs.variable_scope("attention"):
        U = vs.get_variable("U", [decoder_cell_size, attention_embedding_size])
        b = vs.get_variable("b", [attention_embedding_size])
        v = vs.get_variable("v", [attention_embedding_size])

        # Decoder embedding.
        decoder_state.set_shape([batch_size, decoder_cell_size])
        d = nn_ops.xw_plus_b(decoder_state, U, b)
        d = array_ops.reshape(d, [batch_size, 1, 1, attention_embedding_size])

        # Energies.
        e = math_ops.reduce_sum(
            v * math_ops.tanh(encoder_embedding + d), [2, 3])
        if self.model_params.attention_params.type == "window":
          e = attention_mask_ops.attention_mask_window(
              self.encoder_states[-1][1], decoder_time_idx, e)
        elif prev_alignment and self.model_params.attention_params.type == "median":
          window_l = self.model_params.attention_params.median_window_l
          window_r = self.model_params.attention_params.median_window_r
          e = attention_mask_ops.attention_mask_median(
              self.encoder_states[-1][1], e, prev_alignment, window_l=window_l,
              window_r=window_r)
        else:
          e = attention_mask_ops.attention_mask(self.encoder_states[-1][1], e)

        # Alignment.
        a = nn_ops.softmax(e, name="alignment_%d" % (decoder_time_idx))

        # Context.
        c = math_ops.reduce_sum(
            array_ops.reshape(a, [batch_size, len(self.encoder_states[-1][0]), 1, 1]) * encoder_states,
            [1, 2])
        c = array_ops.reshape(c, [batch_size, self.model_params.encoder_cell_size])
        return a, c

    new_states = [None]
    new_attentions = [None]
    new_alignments = [None]
    new_outputs = [emb]
    def create_gru_cell(attention):
      stack_idx = len(new_states)

      # If empty, create new state.
      if len(states):
        state = states[stack_idx]
      elif self.model_params.input_layer == 'placeholder':
        # During decoding, this is given to us.
        state = tf.placeholder(
            tf.float32, shape=(batch_size, decoder_cell_size))
        self.decoder_states_initial.append(state)
      else:
        state = array_ops.zeros([batch_size, decoder_cell_size], tf.float32)

      # The input to this layer is the output of the previous layer.
      x = new_outputs[stack_idx - 1]
      # If the previous timestep has an attention context, concat it.
      if attention:
        if self.model_params.input_layer == "placeholder":
          attention_placeholder = tf.placeholder(
              tf.float32, shape=[batch_size, self.model_params.encoder_cell_size],
              name="attention_%d" % stack_idx)
          self.decoder_attentions_initial.append(attention_placeholder)
          x = array_ops.concat(1, [x, attention_placeholder])
        elif len(attentions):
          x = array_ops.concat(1, [x, attentions[stack_idx]])
        else:
          x = array_ops.concat(1, [x, array_ops.zeros([
              batch_size, self.model_params.encoder_cell_size], tf.float32)])
        x.set_shape([batch_size, new_outputs[stack_idx - 1].get_shape()[1].value + self.model_params.encoder_cell_size])

      # Create our GRU cell.
      _, _, _, _, h = gru_ops.gru_cell(
          decoder_cell_size, self.tokens_len, state, x, time_idx=decoder_time_idx)
      h.set_shape([batch_size, decoder_cell_size])

      new_states.append(h)
      if attention:
        prev_alignment = None
        if self.model_params.input_layer == "placeholder":
          prev_alignment = tf.placeholder(
              tf.float32, shape=[batch_size, len(self.encoder_states[-1][0])],
              name="alignment_%d" % stack_idx)
          self.decoder_alignments_initial.append(prev_alignment)
        elif prev_alignments:
          prev_alignment = prev_alignments[stack_idx]
        a, c = create_attention(h, prev_alignment)
        new_attentions.append(c)
        new_alignments.append(a)
        h = array_ops.concat(1, [h, c])
        h.set_shape(
            [batch_size, self.model_params.decoder_cell_size + self.model_params.encoder_cell_size])
      else:
        new_attentions.append(None)
        new_alignments.append(None)
      new_outputs.append(h)

    if self.model_params.decoder_layer:
      for idx, s in enumerate(self.model_params.decoder_layer):
        with vs.variable_scope(str(idx + 1)):
          create_gru_cell(attention=(s == "attention"))
    else:
      with vs.variable_scope("1"):
        create_gru_cell(attention=True)
      with vs.variable_scope("2"):
        create_gru_cell(attention=False)

    return new_outputs, new_states, new_attentions, new_alignments


  def create_loss(self):
    start_time = time.time()

    self.losses = []

    if not self.model_params.encoder_only:
      self.logits = self.decoder_states

      targets = self.tokens[1:]
      weights = self.tokens_weights[1:]

      log_perps = seq2seq.sequence_loss(
          self.logits, targets, weights, self.model_params.vocab_size)
      self.logperp = log_perps
      self.losses.append(log_perps)

      if self.model_params.pinyin_ext:
        assert self.model_params.pinyin_vocab_size
        pinyin_log_perps = seq2seq.sequence_loss(
            self.logits_pinyin, self.tokens_pinyin[1:], self.tokens_pinyin_weights[1:], self.model_params.pinyin_vocab_size)
        self.losses.append(pinyin_log_perps)

    if self.model_params.encoder_lm:
      # self.create_loss_encoder_lm(self.encoder_states[2][0], self.features_fbank)
      self.create_loss_encoder_copy_and_predict_delta(self.encoder_states[2][0], self.features_fbank)

    print('create_loss graph time %f' % (time.time() - start_time))

  def create_loss_sequence_regression(self, state, labels, state_ws, pred_ws, weight):
    assert len(labels) == len(state_ws)
    assert len(labels) == len(pred_ws)
    predictions = []
    losses = []
    for label, state_w, pred_w in zip(labels, state_ws, pred_ws):
      state = tf.nn.elu(nn_ops.xw_plus_b(state, state_w[0], state_w[1]))
      pred = nn_ops.xw_plus_b(state, pred_w[0], pred_w[1])

      predictions.append(pred)
      losses.append(tf.nn.l2_loss((label - pred) * weight * self.optimization_params.encoder_lm_loss_weight))
    return predictions, losses

  def create_loss_encoder_lm_2(
      self, encoder_states, frames, delay=1, frames_to_predict=10):
    with vs.variable_scope("encoder_lm"):
      state_dim = encoder_states[0].get_shape()[1].value
      prediction_dim = frames[0].get_shape()[1].value

      state_ws = []
      pred_ws = []
      for idx in range(frames_to_predict):
        state_ws.append([vs.get_variable("state_w_%d" % idx, [state_dim, state_dim]),
                         vs.get_variable("state_b_%d" % idx, [state_dim])])
        pred_ws.append([vs.get_variable("pred_w_%d" % idx, [state_dim, prediction_dim]),
                         vs.get_variable("pred_b_%d" % idx, [prediction_dim])])

      self.loss_encoder_lm_losses = []
      for idx in range(len(encoder_states) - delay - frames_to_predict + 1):
        encoder_state = encoder_states[idx]
        labels = frames[idx+delay:idx+delay+frames_to_predict]
        weight = self.features_weight[idx + delay + frames_to_predict - 1]

        predictions, losses = self.create_loss_sequence_regression(
            encoder_state, labels, state_ws, pred_ws, weight)
        self.loss_encoder_lm_losses.extend(losses)
      self.loss_encoder_lm_loss = tf.reduce_sum(tf.add_n(self.loss_encoder_lm_losses)) / (
          tf.reduce_sum(tf.add_n(self.features_weight[:-(delay + frames_to_predict - 1)])))
      self.losses.append(self.loss_encoder_lm_loss)

  def create_loss_encoder_copy_and_predict(
      self, encoder_states, frames, frames_to_predict=10):
    self.encoder_predictions = []
    assert len(encoder_states) == len(frames)
    with vs.variable_scope("encoder_lm"):
      batch_size = self.batch_size
      state_dim = encoder_states[0].get_shape()[1].value
      label_dim = frames[0].get_shape()[1].value
      prediction_dim = label_dim * frames_to_predict * 2

      W = vs.get_variable("W", [state_dim, prediction_dim])
      b = vs.get_variable("b", [prediction_dim])

      self.loss_encoder_lm_losses = []
      for idx in range(frames_to_predict, len(encoder_states) - frames_to_predict):
        encoder_state = encoder_states[idx]
        prediction = nn_ops.xw_plus_b(encoder_state, W, b, name="encoder_lm_prediction_%d" % idx)
        self.encoder_predictions.append(prediction)

        label = array_ops.concat(1, frames[idx - frames_to_predict:idx] + frames[idx + 1:idx + frames_to_predict + 1])

        weight = np.concatenate([np.linspace(10, 1, frames_to_predict), np.linspace(1, 10, frames_to_predict)])
        weight = np.tile(np.repeat(weight, label_dim), [batch_size, 1]) * self.optimization_params.encoder_lm_loss_weight
        weight = tf.convert_to_tensor(weight, dtype=tf.float32)
        loss = tf.nn.l2_loss((label - prediction) * weight)

        loss = loss * self.features_weight[idx + frames_to_predict]
        self.loss_encoder_lm_losses.append(loss)
      self.loss_encoder_lm_loss = tf.reduce_sum(tf.add_n(self.loss_encoder_lm_losses)) / (
          tf.reduce_sum(tf.add_n(self.features_weight[:-(frames_to_predict)])))
      self.losses.append(self.loss_encoder_lm_loss)

  def create_loss_encoder_copy_and_predict_delta(
      self, encoder_states, frames, frames_to_predict=10):
    self.encoder_predictions = []
    assert len(encoder_states) == len(frames)
    with vs.variable_scope("encoder_lm"):
      batch_size = self.batch_size
      state_dim = encoder_states[0].get_shape()[1].value
      label_dim = frames[0].get_shape()[1].value
      prediction_dim = label_dim * frames_to_predict * 2

      W = vs.get_variable("W", [state_dim, prediction_dim])
      b = vs.get_variable("b", [prediction_dim])

      self.loss_encoder_lm_losses = []
      for idx in range(frames_to_predict, len(encoder_states) - frames_to_predict):
        encoder_state = encoder_states[idx]
        prediction = nn_ops.xw_plus_b(encoder_state, W, b, name="encoder_lm_prediction_%d" % idx)
        self.encoder_predictions.append(prediction)

        label = frames[idx - frames_to_predict:idx] + frames[idx + 1:idx + frames_to_predict + 1]
        # We want to predict the delta rather than the absolute -- absolute becomes just a low pass filter.
        label = [l - frames[idx] for l in label]
        label = array_ops.concat(1, label)

        weight = np.concatenate([np.linspace(10, 1, frames_to_predict), np.linspace(1, 10, frames_to_predict)])
        weight = np.tile(np.repeat(weight, label_dim), [batch_size, 1]) * self.optimization_params.encoder_lm_loss_weight
        weight = tf.convert_to_tensor(weight, dtype=tf.float32)
        loss = tf.nn.l2_loss((label - prediction) * weight)

        loss = loss * self.features_weight[idx + frames_to_predict]
        self.loss_encoder_lm_losses.append(loss)
      self.loss_encoder_lm_loss = tf.reduce_sum(tf.add_n(self.loss_encoder_lm_losses)) / (
          tf.reduce_sum(tf.add_n(self.features_weight[:-(frames_to_predict)])))
      self.losses.append(self.loss_encoder_lm_loss)

  def create_loss_encoder_lm(
      self, encoder_states, frames, delay=1, frames_to_predict=10):
    self.encoder_predictions = []
    assert len(encoder_states) == len(frames)
    with vs.variable_scope("encoder_lm"):
      batch_size = self.batch_size
      state_dim = encoder_states[0].get_shape()[1].value
      label_dim = frames[0].get_shape()[1].value
      prediction_dim = label_dim * frames_to_predict


      W = vs.get_variable("W", [state_dim, prediction_dim])
      b = vs.get_variable("b", [prediction_dim])

      self.loss_encoder_lm_losses = []
      for idx in range(len(encoder_states) - delay - frames_to_predict + 1):
        encoder_state = encoder_states[idx]
        prediction = nn_ops.xw_plus_b(encoder_state, W, b, name="encoder_lm_prediction_%d" % idx)
        self.encoder_predictions.append(prediction)

        if frames_to_predict == 1:
          label = frames[idx + delay]
        else:
          label = array_ops.concat(1, frames[idx+delay:idx+delay+frames_to_predict])

        weight = np.tile(np.repeat(np.linspace(1, 10, frames_to_predict), label_dim), [batch_size, 1]) * self.optimization_params.encoder_lm_loss_weight
        weight = tf.convert_to_tensor(weight, dtype=tf.float32)
        loss = tf.nn.l2_loss((label - prediction) * weight)

        loss = loss * self.features_weight[idx + delay + frames_to_predict - 1]
        self.loss_encoder_lm_losses.append(loss)
      self.loss_encoder_lm_loss = tf.reduce_sum(tf.add_n(self.loss_encoder_lm_losses)) / (
          tf.reduce_sum(tf.add_n(self.features_weight[:-(delay + frames_to_predict - 1)])))
      self.losses.append(self.loss_encoder_lm_loss)

  def create_optimizer(self):
    start_time = time.time()

    params = tf.trainable_variables()
    #for idx, param in enumerate(params):
    #  print('param %d: %s %s' % (idx, param.name, str(param.get_shape())))

    self.updates = []

    grads = tf.gradients(self.losses, params)

    if self.optimization_params.gaussian_noise_stddev:
      ngrads = []
      for weight, grad in zip(params, grads):
        if isinstance(grad, tf.Tensor):
          noise = tf.random_normal(
              weight.get_shape(),
              stddev=self.optimization_params.gaussian_noise_stddev,
              seed=self.global_epochs)
          ngrad = grad + noise
        else:
          ngrad = grad
        ngrads.append(ngrad)
      grads = ngrads

    if self.optimization_params.max_gradient_norm:
      cgrads, norm = clip_ops.clip_by_global_norm(
          grads, self.optimization_params.max_gradient_norm, name="clip_gradients")
      self.gradient_norm = norm

    if self.optimization_params.type == "adagrad":
      opt = tf.train.AdagradOptimizer(
          learning_rate=self.optimization_params.adagrad.learning_rate,
          initial_accumulator_value=self.optimization_params.adagrad.initial_accumulator_value)
    elif self.optimization_params.type == "adadelta":
      opt = tf.train.AdadeltaOptimizer(
          learning_rate=self.optimization_params.adadelta.learning_rate,
          decay_rate=self.optimization_params.adadelta.decay_rate,
          epsilon=self.optimization_params.adadelta.epsilon)
    elif self.optimization_params.type == "adam":
      opt = tf.train.AdamOptimizer(
          learning_rate=self.optimization_params.adam.learning_rate,
          beta1=self.optimization_params.adam.beta1,
          beta2=self.optimization_params.adam.beta2,
          epsilon=self.optimization_params.adam.epsilon)
    elif self.optimization_params.type == "gd":
      opt = tf.train.GradientDescentOptimizer(
          learning_rate=self.optimization_params.gd.learning_rate)
    else:
      raise ValueError(
          "Unknown optimization type: %s" % str(self.optimization_params))

    self.updates.append(opt.apply_gradients(
        zip(cgrads, params), global_step=self.global_step))

    print('create_optimizer graph time %f' % (time.time() - start_time))


  def run_graph(self, sess, targets, feed_dict=None):
    fetches = []
    for name, target in targets.iteritems():
      if isinstance(target, (list, tuple)):
        fetches.extend(target)
      else:
        fetches.append(target)
    r = sess.run(fetches, feed_dict)

    f = {}
    start = 0
    for name, target in targets.iteritems():
      length = 1
      if isinstance(target, (list, tuple)):
        length = len(target)
      end = start + length
      if isinstance(target, (list, tuple)):
        f[name] = r[start:end]
      else:
        f[name] = r[start]
      start = end
    return f


  def compute_accuracy(self, logits, targets, weights):
    assert len(logits) == len(targets)
    assert len(logits) == len(weights)
    correct = 0.0
    count = 0.0
    for logit, target, weight in zip(logits, targets, weights):
      correct = correct + (np.equal(logit.argmax(axis=1), np.array(target)).astype(float) * weight).sum()
      count = count + weight.sum()

    self.epoch_correct = self.epoch_correct + correct
    self.epoch_count = self.epoch_count + count
    self.epoch_accuracy = self.epoch_correct / self.epoch_count

    return correct / count


  def step_epoch(self, sess, forward_only):
    steps_per_epoch = int(math.ceil(self.dataset_size // self.batch_size))

    self.epoch_correct = 0
    self.epoch_count = 0

    for s in range(steps_per_epoch):
      self.step(sess, forward_only)
    self.epochs = self.step_total / steps_per_epoch

    if not forward_only:
      print("logdir: %s" % self.logdir)
      self.saver.save(
          sess, os.path.join(self.logdir, 'ckpt'),
          global_step=self.epochs + self.global_epochs)

    results_proto = speech4_pb2.ResultsProto()
    acc_proto = results_proto.acc
    acc_proto.pos = int(self.epoch_correct)
    acc_proto.count = int(self.epoch_count)

    with open(os.path.join(self.logdir, 'results_%d.pbtxt' % self.epochs), 'w') as proto_file:
      proto_file.write(str(results_proto))

    print("*** epoch done %d ***" % (self.epochs + self.global_epochs))
    print("step_total %d, avg_step_time: %f, accuracy %f" % (
        self.step_total, self.avg_step_time, self.epoch_accuracy))
    print("*** epoch done %d ***" % self.epochs)


  def step(self, sess, forward_only):
    start_time = time.time()

    steps_per_report = 100
    report = forward_only or (self.step_total % steps_per_report == 0)

    targets = {}
    if report:
      #targets['features_fbank'] = self.features_fbank
      targets['uttid'] = self.uttid
      targets['text'] = self.text
      targets['tokens'] = self.tokens
      targets['tokens_weights'] = self.tokens_weights
      if not self.model_params.encoder_only:
        targets['logperp'] = self.logperp
        targets['logits'] = self.logits

      if self.model_params.encoder_lm:
        targets['encoder_lm_loss'] = self.loss_encoder_lm_loss

      if self.visualization_params.attention:
        pass
      if self.visualization_params.encoder_predictions:
        targets["features_fbank"] = self.features_fbank
        targets["encoder_predictions"] = self.encoder_predictions

    if not forward_only and report:
      targets['gradient_norm'] = self.gradient_norm

    if not forward_only:
      targets['updates'] = self.updates

    fetches = self.run_graph(sess, targets)

    step_time = time.time() - start_time
    self.step_total += 1
    self.step_time_total += step_time
    steps_per_epoch = int(math.ceil(self.dataset_size // self.batch_size))
    self.epochs = self.step_total / steps_per_epoch

    self.avg_step_time = self.step_time_total / self.step_total

    if report:
      logperp = 0

      # Save our filter banks.
      #features_fbank = fetches["features_fbank"]
      #features_fbank = [x[0,:] for x in features_fbank]
      #features_fbank = np.transpose(np.vstack(features_fbank))
      #np.save(os.path.join(self.logdir, "features_fbank"), features_fbank)

      if 'logperp' in fetches:
        logperp = fetches['logperp']

      if "encoder_lm_loss" in fetches:
        print fetches["encoder_lm_loss"]

      # Save the visualization of our predictions compared to the original to
      # disk.
      if self.visualization_params.encoder_predictions:
        features_fbank = fetches["features_fbank"]
        encoder_predictions = fetches["encoder_predictions"]
        features_fbank = [x[0,:] for x in features_fbank]
        encoder_predictions = [x[0,:] for x in encoder_predictions]
        np.save(os.path.join(self.logdir, "features_fbank"),
                np.vstack(features_fbank))
        np.save(os.path.join(self.logdir, "encoder_predictions"),
                np.vstack(encoder_predictions))

      perplexity = np.exp(logperp)
      gradient_norm = 0.0
      if 'gradient_norm' in fetches:
        gradient_norm = fetches['gradient_norm']
      
      accuracy = 0.0
      if 'logits' in fetches:
        accuracy = self.compute_accuracy(
            fetches['logits'], fetches['tokens'][1:], fetches['tokens_weights'][1:])

      if not forward_only:
        self.saver.save(
            sess, os.path.join(self.logdir, 'ckpt'), global_step=self.epochs + self.global_epochs + 1)

      print("step_total %d, avg_step_time: %f, accuracy %f, perplexity %f, gradient_norm %f" % (
          self.step_total, self.avg_step_time, accuracy, perplexity, gradient_norm))

    return fetches
