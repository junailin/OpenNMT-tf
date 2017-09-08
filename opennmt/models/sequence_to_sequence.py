"""Standard sequence-to-sequence model."""

import tensorflow as tf

import opennmt.constants as constants

from opennmt.models.model import Model
from opennmt.utils.losses import masked_sequence_loss


class SequenceToSequence(Model):

  def __init__(self,
               source_inputter,
               target_inputter,
               encoder,
               decoder,
               name="seq2seq"):
    """Initializes a sequence-to-sequence model.

    Args:
      source_inputter: A `onmt.inputters.Inputter` to process the source data.
      target_inputter: A `onmt.inputters.Inputter` to process the target data.
        Currently, only the `onmt.inputters.WordEmbedder` is supported.
      encoder: A `onmt.encoders.Encoder` to encode the source.
      decoder: A `onmt.decoders.Decoder` to decode the target.
      name: The name of this model.
    """
    super(SequenceToSequence, self).__init__(name)

    self.encoder = encoder
    self.decoder = decoder

    self.source_inputter = source_inputter
    self.target_inputter = target_inputter

  def _shift_target(self, labels):
    """Generate shifted target sequences with <s> and </s>."""
    bos = tf.cast(tf.constant([constants.START_OF_SENTENCE_ID]), tf.int64)
    eos = tf.cast(tf.constant([constants.END_OF_SENTENCE_ID]), tf.int64)

    ids = labels["ids"]
    length = labels["length"]

    labels = self.target_inputter.set_data_field(
      labels,
      "ids_out",
      tf.concat([ids, eos], axis=0),
      padded_shape=[None])
    labels = self.target_inputter.set_data_field(
      labels,
      "ids",
      tf.concat([bos, ids], axis=0),
      padded_shape=[None])

    # Increment length accordingly.
    self.target_inputter.set_data_field(labels, "length", length + 1)

    return labels

  def _build_features(self, features_file, metadata={}):
    dataset = self.source_inputter.make_dataset(features_file, metadata)
    return dataset, self.source_inputter.padded_shapes

  def _build_labels(self, labels_file=None, metadata={}):
    if labels_file is None:
      self.target_inputter.initialize(metadata)
      return None, None
    else:
      dataset = self.target_inputter.make_dataset(labels_file, metadata)
      dataset = dataset.map(self._shift_target)
      return dataset, self.target_inputter.padded_shapes

  def _build(self, features, labels, params, mode):
    batch_size = tf.shape(features["length"])[0]

    with tf.variable_scope("encoder"):
      source_inputs = self.source_inputter.transform_data(
        features,
        mode,
        log_dir=params.get("log_dir"))

      encoder_outputs, encoder_states, encoder_sequence_length = self.encoder.encode(
        source_inputs,
        sequence_length=features["length"],
        mode=mode)

    with tf.variable_scope("decoder") as decoder_scope:
      embedding_fn = lambda x: self.target_inputter.transform(
        x,
        mode,
        scope=decoder_scope,
        reuse_next=True)

      if mode != tf.estimator.ModeKeys.PREDICT:
        target_inputs = self.target_inputter.transform_data(
          labels,
          mode,
          log_dir=params.get("log_dir"))

        decoder_outputs, _, decoded_length = self.decoder.decode(
          target_inputs,
          labels["length"],
          self.target_inputter.vocabulary_size,
          encoder_states=encoder_states,
          scheduled_sampling_probability=params["scheduled_sampling_probability"],
          embeddings=embedding_fn,
          mode=mode,
          memory=encoder_outputs,
          memory_sequence_length=encoder_sequence_length)
      elif "beam_width" in params and params["beam_width"] == 1:
        decoder_outputs, _, decoded_length = self.decoder.dynamic_decode(
          embedding_fn,
          tf.fill([batch_size], constants.START_OF_SENTENCE_ID),
          constants.END_OF_SENTENCE_ID,
          self.target_inputter.vocabulary_size,
          encoder_states,
          maximum_iterations=params["maximum_iterations"],
          mode=mode,
          memory=encoder_outputs,
          memory_sequence_length=encoder_sequence_length)
      else:
        decoder_outputs, _, decoded_length = self.decoder.dynamic_decode_and_search(
          embedding_fn,
          tf.fill([batch_size], constants.START_OF_SENTENCE_ID),
          constants.END_OF_SENTENCE_ID,
          self.target_inputter.vocabulary_size,
          encoder_states,
          beam_width=params["beam_width"],
          length_penalty=params["length_penalty"],
          maximum_iterations=params["maximum_iterations"],
          mode=mode,
          memory=encoder_outputs,
          memory_sequence_length=encoder_sequence_length)

    if mode != tf.estimator.ModeKeys.PREDICT:
      loss = masked_sequence_loss(
        decoder_outputs,
        labels["ids_out"],
        labels["length"])

      return tf.estimator.EstimatorSpec(
        mode,
        loss=loss,
        train_op=self._build_train_op(loss, params))
    else:
      target_vocab_rev = tf.contrib.lookup.index_to_string_table_from_file(
        self.target_inputter.vocabulary_file,
        vocab_size=self.target_inputter.vocabulary_size - self.target_inputter.num_oov_buckets,
        default_value=constants.UNKNOWN_TOKEN)
      predictions = {}
      predictions["tokens"] = target_vocab_rev.lookup(tf.cast(decoder_outputs, tf.int64))
      predictions["length"] = decoded_length

      return tf.estimator.EstimatorSpec(
        mode,
        predictions=predictions)

  def format_prediction(self, prediction, params=None):
    n_best = params and params.get("n_best")
    n_best = n_best or 1

    if n_best > len(prediction["tokens"]):
      raise ValueError("n_best cannot be greater than beam_width")

    all_preds = []
    for i in range(n_best):
      tokens = prediction["tokens"][i][:prediction["length"][i] - 1] # Ignore </s>.
      sentence = b" ".join(tokens)
      sentence = sentence.decode('utf-8')
      all_preds.append(sentence)

    return all_preds
