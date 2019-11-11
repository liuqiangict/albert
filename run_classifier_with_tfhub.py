# coding=utf-8
# Copyright 2019 The Google Research Authors.
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

"""ALBERT finetuning runner with TF-Hub."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from sklearn.metrics import roc_auc_score

import tensorflow as tf
import tensorflow_hub as hub

#from albert import optimization
#from albert import run_classifier_sp
#from albert import tokenization
import optimization
import run_classifier_sp
import tokenization

flags = tf.flags

FLAGS = flags.FLAGS

flags.DEFINE_string(
    "albert_hub_module_handle", None,
    "Handle for the ALBERT TF-Hub module.")


def create_model(is_training, input_ids, input_mask, segment_ids, labels,
                 num_labels, albert_hub_module_handle):
  """Creates a classification model."""
  tags = set()
  if is_training:
    tags.add("train")
  albert_module = hub.Module(albert_hub_module_handle, tags=tags,
                             trainable=True)
  albert_inputs = dict(
      input_ids=input_ids,
      input_mask=input_mask,
      segment_ids=segment_ids)
  albert_outputs = albert_module(
      inputs=albert_inputs,
      signature="tokens",
      as_dict=True)

  # In the demo, we are doing a simple classification task on the entire
  # segment.
  #
  # If you want to use the token-level output, use
  # albert_outputs["sequence_output"] instead.
  output_layer = albert_outputs["pooled_output"]

  hidden_size = output_layer.shape[-1].value

  output_weights = tf.get_variable(
      "output_weights", [num_labels, hidden_size],
      initializer=tf.truncated_normal_initializer(stddev=0.02))

  output_bias = tf.get_variable(
      "output_bias", [num_labels], initializer=tf.zeros_initializer())

  with tf.variable_scope("loss"):
    if is_training:
      # I.e., 0.1 dropout
      output_layer = tf.nn.dropout(output_layer, keep_prob=0.9)

    logits = tf.matmul(output_layer, output_weights, transpose_b=True)
    logits = tf.nn.bias_add(logits, output_bias)
    probabilities = tf.nn.softmax(logits, axis=-1)
    log_probs = tf.nn.log_softmax(logits, axis=-1)

    one_hot_labels = tf.one_hot(labels, depth=num_labels, dtype=tf.float32)

    per_example_loss = -tf.reduce_sum(one_hot_labels * log_probs, axis=-1)
    loss = tf.reduce_mean(per_example_loss)

    return (loss, per_example_loss, logits, probabilities)


def model_fn_builder(num_labels, learning_rate, num_train_steps,
                     num_warmup_steps, use_tpu, albert_hub_module_handle):
  """Returns `model_fn` closure for TPUEstimator."""

  def model_fn(features, labels, mode, params):  # pylint: disable=unused-argument
    """The `model_fn` for TPUEstimator."""

    tf.logging.info("*** Features ***")
    for name in sorted(features.keys()):
      tf.logging.info("  name = %s, shape = %s" % (name, features[name].shape))

    guid = features["guid"]
    input_ids = features["input_ids"]
    input_mask = features["input_mask"]
    segment_ids = features["segment_ids"]
    label_ids = features["label_ids"]

    is_training = (mode == tf.estimator.ModeKeys.TRAIN)

    (total_loss, per_example_loss, logits, probabilities) = create_model(
        is_training, input_ids, input_mask, segment_ids, label_ids, num_labels,
        albert_hub_module_handle)

    output_spec = None
    if mode == tf.estimator.ModeKeys.TRAIN:
      global_step, train_op, update_learning_rate = optimization.create_optimizer(
          total_loss, learning_rate, num_train_steps, num_warmup_steps, use_tpu)

      logging_hook = tf.train.LoggingTensorHook({"loss": total_loss, "learning_rate" : update_learning_rate, "global_step": global_step}, every_n_iter=FLAGS.hooking_frequence)
      output_spec = tf.contrib.tpu.TPUEstimatorSpec(
          mode=mode,
          loss=total_loss,
          train_op=train_op,
          training_hooks=[logging_hook])
    elif mode == tf.estimator.ModeKeys.EVAL:

      def metric_fn(per_example_loss, label_ids, logits):
        predictions = tf.argmax(logits, axis=-1, output_type=tf.int32)
        accuracy = tf.metrics.accuracy(label_ids, predictions)
        loss = tf.metrics.mean(per_example_loss)
        return {
            "eval_accuracy": accuracy,
            "eval_loss": loss,
        }

      eval_metrics = (metric_fn, [per_example_loss, label_ids, logits])
      output_spec = tf.contrib.tpu.TPUEstimatorSpec(
          mode=mode,
          loss=total_loss,
          eval_metrics=eval_metrics)
    elif mode == tf.estimator.ModeKeys.PREDICT:
      output_spec = tf.contrib.tpu.TPUEstimatorSpec(
          mode=mode, 
          predictions={
            "guid": guid,
            "probabilities": probabilities,
            #"predictions": predictions,
            "labels": label_ids            
            }
        )
    else:
      raise ValueError(
          "Only TRAIN, EVAL and PREDICT modes are supported: %s" % (mode))

    return output_spec

  return model_fn


def create_tokenizer_from_hub_module(albert_hub_module_handle):
  """Get the vocab file and casing info from the Hub module."""
  with tf.Graph().as_default():
    albert_module = hub.Module(albert_hub_module_handle)
    tokenization_info = albert_module(signature="tokenization_info",
                                      as_dict=True)
    with tf.Session() as sess:
      vocab_file, do_lower_case = sess.run([tokenization_info["vocab_file"],
                                            tokenization_info["do_lower_case"]])

  print('*' * 100)
  print('vocab_file:', vocab_file)
  print('spm_model_file:', FLAGS.spm_model_file)
  print('*' * 100)
  return tokenization.FullTokenizer(
      vocab_file=vocab_file, do_lower_case=do_lower_case,
      spm_model_file=FLAGS.spm_model_file)


def main(_):
  tf.logging.set_verbosity(tf.logging.INFO)

  processors = {
      "cola": run_classifier_sp.ColaProcessor,
      "mnli": run_classifier_sp.MnliProcessor,
      "mrpc": run_classifier_sp.MrpcProcessor,
      "qp": run_classifier_sp.QPProcessor,
  }

  if not FLAGS.do_train and not FLAGS.do_eval and not FLAGS.do_predict:
    raise ValueError("At least one of `do_train` or `do_eval` must be True.")

  tf.gfile.MakeDirs(FLAGS.output_dir)

  task_name = FLAGS.task_name.lower()

  if task_name not in processors:
    raise ValueError("Task not found: %s" % (task_name))

  processor = processors[task_name]()

  label_list = processor.get_labels()

  tokenizer = create_tokenizer_from_hub_module(FLAGS.albert_hub_module_handle)

  tpu_cluster_resolver = None
  if FLAGS.use_tpu and FLAGS.tpu_name:
    tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
        FLAGS.tpu_name, zone=FLAGS.tpu_zone, project=FLAGS.gcp_project)

  is_per_host = tf.contrib.tpu.InputPipelineConfig.PER_HOST_V2
  run_config = tf.contrib.tpu.RunConfig(
      cluster=tpu_cluster_resolver,
      master=FLAGS.master,
      model_dir=FLAGS.output_dir,
      #model_dir=FLAGS.input_previous_model_path,
      save_checkpoints_steps=FLAGS.save_checkpoints_steps,
      keep_checkpoint_max=FLAGS.keep_checkpoint_max,
      log_step_count_steps=FLAGS.log_step_count_steps,
      tpu_config=tf.contrib.tpu.TPUConfig(
          iterations_per_loop=FLAGS.iterations_per_loop,
          num_shards=FLAGS.num_tpu_cores,
          per_host_input_for_training=is_per_host))

  train_examples = None
  num_train_steps = None
  num_warmup_steps = None
  if FLAGS.do_train:
    train_examples = processor.get_train_examples(FLAGS.trainnig_data_dir)
    num_train_steps = int(
        len(train_examples) / FLAGS.train_batch_size * FLAGS.num_train_epochs)
    num_warmup_steps = int(num_train_steps * FLAGS.warmup_proportion)

  model_fn = model_fn_builder(
      num_labels=len(label_list),
      learning_rate=FLAGS.learning_rate,
      num_train_steps=num_train_steps,
      num_warmup_steps=num_warmup_steps,
      use_tpu=FLAGS.use_tpu,
      albert_hub_module_handle=FLAGS.albert_hub_module_handle)

  # If TPU is not available, this will fall back to normal Estimator on CPU
  # or GPU.
  estimator = tf.contrib.tpu.TPUEstimator(
      use_tpu=FLAGS.use_tpu,
      model_fn=model_fn,
      config=run_config,
      train_batch_size=FLAGS.train_batch_size,
      eval_batch_size=FLAGS.eval_batch_size,
      predict_batch_size=FLAGS.predict_batch_size)

  if FLAGS.do_train:
    train_file = os.path.join(FLAGS.output_dir, "train.tf_record")
    if not tf.gfile.Exists(train_file):
        train_features = run_classifier_sp.file_based_convert_examples_to_features(train_examples, label_list, FLAGS.max_seq_length, tokenizer, train_file)
    tf.logging.info("***** Running training *****")
    tf.logging.info("  Num examples = %d", len(train_examples))
    tf.logging.info("  Batch size = %d", FLAGS.train_batch_size)
    tf.logging.info("  Num steps = %d", num_train_steps)
    train_input_fn = run_classifier_sp.file_based_input_fn_builder(
        input_file=train_file,
        seq_length=FLAGS.max_seq_length,
        is_training=True,
        drop_remainder=True)
    estimator.train(input_fn=train_input_fn, max_steps=num_train_steps)

  if FLAGS.do_eval:
    eval_examples = processor.get_dev_examples(FLAGS.validation_data_dir)
    eval_features = run_classifier_sp.convert_examples_to_features(
        eval_examples, label_list, FLAGS.max_seq_length, tokenizer)

    tf.logging.info("***** Running evaluation *****")
    tf.logging.info("  Num examples = %d", len(eval_examples))
    tf.logging.info("  Batch size = %d", FLAGS.eval_batch_size)

    # This tells the estimator to run through the entire set.
    eval_steps = None
    # However, if running eval on the TPU, you will need to specify the
    # number of steps.
    if FLAGS.use_tpu:
      # Eval will be slightly WRONG on the TPU because it will truncate
      # the last batch.
      eval_steps = int(len(eval_examples) / FLAGS.eval_batch_size)

    eval_drop_remainder = True if FLAGS.use_tpu else False
    eval_input_fn = run_classifier_sp.input_fn_builder(
        features=eval_features,
        seq_length=FLAGS.max_seq_length,
        is_training=False,
        drop_remainder=eval_drop_remainder)

    result = estimator.evaluate(input_fn=eval_input_fn, steps=eval_steps)

    output_eval_file = os.path.join(FLAGS.output_dir, "eval_results.txt")
    with tf.gfile.GFile(output_eval_file, "w") as writer:
      tf.logging.info("***** Eval results *****")
      for key in sorted(result.keys()):
        tf.logging.info("  %s = %s", key, str(result[key]))
        writer.write("%s = %s\n" % (key, str(result[key])))

  if FLAGS.do_predict:
    '''
    predict_examples = processor.get_test_examples(FLAGS.prediction_data_dir)
    num_actual_predict_examples = len(predict_examples)
    if FLAGS.use_tpu:
      while len(predict_examples) % FLAGS.predict_batch_size != 0:
        predict_examples.append(PaddingInputExample())

    predict_file = os.path.join(FLAGS.output_dir, "predict.tf_record")
    run_classifier_sp.file_based_convert_examples_to_features(predict_examples, label_list, FLAGS.max_seq_length, tokenizer, predict_file)
    '''

    evals = {
        'google': './data/eval/tf_records/google.tf_record',
        'bing_ann': './data/eval/tf_records/bing_ann.tf_record',
        'uhrs': './data/eval/tf_records/uhrs.tf_record',
        'panelone_5k': './data/eval/tf_records/panelone_5k.tf_record',
        'adversial': './data/eval/tf_records/adverserial.tf_record',
        'speller_checked': './data/eval/tf_records/speller_checked.tf_record',
        'speller_usertyped': './data/eval/tf_records/speller_usertyped.tf_record',
    }


    for eval_name, predict_file in evals.items():
      tf.logging.info("***** Running prediction*****")
      #print("Eval name: ", eval_name)
      #print("Eval file: ", predict_file)
      tf.logging.info("  Batch size = %d", FLAGS.predict_batch_size)
      predict_drop_remainder = True if FLAGS.use_tpu else False
      predict_input_fn = run_classifier_sp.file_based_input_fn_builder(
          input_file=predict_file,
          seq_length=FLAGS.max_seq_length,
          is_training=False,
          drop_remainder=predict_drop_remainder)

      result = estimator.predict(input_fn=predict_input_fn)

      #output_predict_file = os.path.join(FLAGS.output_dir, eval_name + "_eval.tsv")
      output_predict_file = os.path.join(FLAGS.output_dir, eval_name + "eval.tsv")
      labels = []
      scores = []
      with tf.gfile.GFile(output_predict_file, "w") as pred_writer:
        num_written_lines = 0
        tf.logging.info("***** Predict results *****")
        for (i, prediction) in enumerate(result):
          guid = prediction["guid"]
          probabilities = prediction["probabilities"]
          label = prediction["labels"]
          output_line = guid.decode("utf-8") + "\t" + str(label) + '\t' + "\t".join(str(class_probability) for class_probability in probabilities) + "\n"
          pred_writer.write(output_line)
          labels.append(label)
          scores.append(probabilities[1])
        auc = roc_auc_score(labels, scores)
        print(eval_name, ':\t', auc)

if __name__ == "__main__":
  #flags.mark_flag_as_required("data_dir")
  flags.mark_flag_as_required("task_name")
  flags.mark_flag_as_required("albert_hub_module_handle")
  flags.mark_flag_as_required("output_dir")
  tf.app.run()
