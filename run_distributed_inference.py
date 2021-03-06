#!/usr/bin/env python

# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Runs FFN inference within a dense bounding box.

Inference is performed within a single process.
"""

import os
import time

from google.protobuf import text_format
from absl import app
from absl import flags
import logging
import os
import glob
import numpy as np
import sys
import tensorflow as tf
from tqdm import tqdm

from ffn.utils import bounding_box_pb2
from ffn.inference import inference
from ffn.inference import inference_flags
from ffn.utils import bounding_box
from ffn.utils import geom_utils

from mpi4py import MPI
mpi_comm = MPI.COMM_WORLD
mpi_rank = mpi_comm.Get_rank()
mpi_size = mpi_comm.Get_size()

gfile = tf.io.gfile
tf.compat.v1.disable_eager_execution()

FLAGS = flags.FLAGS

flags.DEFINE_string('bounding_box', None,
                    'BoundingBox proto in text format defining the area '
                    'to segmented.')
flags.DEFINE_list('subvolume_size', '512,512,128', '"valid"subvolume_size to issue to each runner')
flags.DEFINE_list('overlap', '32,32,16', 'overlap of bbox')
flags.DEFINE_string('output_dir', None, 'Enforce output dir regardless of config')
flags.DEFINE_boolean('use_cpu', False, 'Use CPU instead of GPU')
flags.DEFINE_integer('num_gpu', 0, 'Allocate on different GPUs')
flags.DEFINE_boolean('resume', False, 'Whether resuming from cpoints')
flags.DEFINE_boolean('verbose', False, 'logging level')



def divide_bounding_box(bbox, subvolume_size, overlap):
  """divide up into valid subvolumes."""
  # deal with parsed bbox missing "end" attr
  start = geom_utils.ToNumpy3Vector(bbox.start)
  size = geom_utils.ToNumpy3Vector(bbox.size)

  bbox = bounding_box.BoundingBox(start, size)

  calc = bounding_box.OrderlyOverlappingCalculator(
    outer_box=bbox, 
    sub_box_size=subvolume_size, 
    overlap=overlap, 
    include_small_sub_boxes=True,
    back_shift_small_sub_boxes=False)
  
  return [bb for bb in calc.generate_sub_boxes()]
def find_unfinished(sub_bboxes, root_output_dir):
  filtered_sub_bboxes = []
  for sub_bbox in sub_bboxes:
    out_name = 'seg-%d_%d_%d_%d_%d_%d' % (
      sub_bbox.start[0], sub_bbox.start[1], sub_bbox.start[2], 
      sub_bbox.size[0], sub_bbox.size[1], sub_bbox.size[2])
    segmentation_output_dir = os.path.join(root_output_dir, out_name)
    npzs = glob.glob(os.path.join(segmentation_output_dir, '**/*.npz'), recursive=True)
    if len(npzs):
      continue
    else:
      filtered_sub_bboxes.append(sub_bbox)
  logging.warning('pre-post filter: %s %s', len(sub_bboxes), len(filtered_sub_bboxes))
  return filtered_sub_bboxes


def main(unused_argv):
  print('log_level', FLAGS.verbose)
  if FLAGS.verbose:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
  else:
    logger = logging.getLogger()
    logger.setLevel(logging.WARNING)


  start_time = time.time()
  request = inference_flags.request_from_flags()
  if mpi_rank == 0:
    if not gfile.exists(request.segmentation_output_dir):
      gfile.makedirs(request.segmentation_output_dir)
    if FLAGS.output_dir is None:
      root_output_dir = request.segmentation_output_dir
    else:
      root_output_dir = FLAGS.output_dir

    bbox = bounding_box_pb2.BoundingBox()
    text_format.Parse(FLAGS.bounding_box, bbox)

    subvolume_size = np.array([int(i) for i in FLAGS.subvolume_size])
    overlap = np.array([int(i) for i in FLAGS.overlap])
    sub_bboxes = divide_bounding_box(bbox, subvolume_size, overlap)
    if FLAGS.resume:
      sub_bboxes = find_unfinished(sub_bboxes, root_output_dir)

    sub_bboxes = np.array_split(np.array(sub_bboxes), mpi_size)
  else:
    sub_bboxes = None
    root_output_dir = None
  
  sub_bboxes = mpi_comm.scatter(sub_bboxes, 0)
  root_output_dir = mpi_comm.bcast(root_output_dir, 0)
  
  for sub_bbox in sub_bboxes:
    out_name = 'seg-%d_%d_%d_%d_%d_%d' % (
      sub_bbox.start[0], sub_bbox.start[1], sub_bbox.start[2], 
      sub_bbox.size[0], sub_bbox.size[1], sub_bbox.size[2])
    segmentation_output_dir = os.path.join(root_output_dir, out_name)
    request.segmentation_output_dir = segmentation_output_dir
    if FLAGS.num_gpu > 0:
      use_gpu = str(mpi_rank % FLAGS.num_gpu)
    else:
      use_gpu = ''
    runner = inference.Runner(use_cpu=FLAGS.use_cpu, use_gpu=use_gpu)
    cube_start_time = (time.time() - start_time) / 60
    runner.start(request)
    runner.run(sub_bbox.start[::-1], sub_bbox.size[::-1])
    cube_finish_time = (time.time() - start_time) / 60
    print('%s finished in %s min' % (out_name, cube_finish_time - cube_start_time))
    runner.stop_executor()

  counter_path = os.path.join(request.segmentation_output_dir, 'counters_%d.txt' % mpi_rank)
  if not gfile.exists(counter_path):
    runner.counters.dump(counter_path)

if __name__ == '__main__':
  app.run(main)
