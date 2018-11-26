from __future__ import absolute_import
# --------------------------------------------------------
# Faster R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick and Sean Bell
# --------------------------------------------------------
# --------------------------------------------------------
# Reorganized and modified by Jianwei Yang and Jiasen Lu
# --------------------------------------------------------

import torch
import torch.nn as nn
import numpy as np
import math
import yaml
from model.utils.config import cfg
from .generate_anchors import generate_anchors
from .bbox_transform import bbox_transform_inv, clip_boxes, clip_boxes_batch, bbox_overlaps_batch
from model.nms.nms_wrapper import nms

import pdb

DEBUG = False

class _ProposalLayer(nn.Module):
    """
    Outputs object detection proposals by applying estimated bounding-box
    transformations to a set of regular boxes (called "anchors").
    """

    def \
            __init__(self, feat_stride, scales, ratios):
        super(_ProposalLayer, self).__init__()

        self._feat_stride = feat_stride
        self._anchors = torch.from_numpy(generate_anchors(base_size=feat_stride, scales=np.array(scales),
            ratios=np.array(ratios))).float()
        self._num_anchors = self._anchors.size(0)

        # rois blob: holds R regions of interest, each is a 5-tuple
        # (n, x1, y1, x2, y2) specifying an image batch index n and a
        # rectangle (x1, y1, x2, y2)
        # top[0].reshape(1, 5)
        #
        # # scores blob: holds scores for R regions of interest
        # if len(top) > 1:
        #     top[1].reshape(1, 1, 1, 1)

    def forward(self, input):

        # Algorithm:
        #
        # for each (H, W) location i
        #   generate A anchor boxes centered on cell i
        #   apply predicted bbox deltas at cell i to each of the A anchors
        # clip predicted boxes to image
        # remove predicted boxes with either height or width < threshold
        # sort all (proposal, score) pairs by score from highest to lowest
        # take top pre_nms_topN proposals before NMS
        # apply NMS with threshold 0.7 to remaining proposals
        # take after_nms_topN proposals after NMS
        # return the top proposals (-> RoIs top, scores top)


        # the first set of _num_anchors channels are bg probs
        # the second set are the fg probs
        scores = input[0][:, self._num_anchors:, :, :] # [1,11,128,256]
        bbox_deltas = input[1] # [1,44,128,256]
        im_info = input[2] # [1,3]
        cfg_key = input[3]
        ignore_region = input[4] # [1,30,4]
        num_ignore = input[5]

        pre_nms_topN  = cfg[cfg_key].RPN_PRE_NMS_TOP_N # 12000
        post_nms_topN = cfg[cfg_key].RPN_POST_NMS_TOP_N # 2000
        nms_thresh    = cfg[cfg_key].RPN_NMS_THRESH # 0.7
        min_size      = cfg[cfg_key].RPN_MIN_SIZE # 8

        batch_size = bbox_deltas.size(0) # 1

        feat_height, feat_width = scores.size(2), scores.size(3) # 128 , 256
        shift_x = np.arange(0, feat_width) * self._feat_stride
        shift_y = np.arange(0, feat_height) * self._feat_stride
        shift_x, shift_y = np.meshgrid(shift_x, shift_y) # [128,256] [128,256]
        shifts = torch.from_numpy(np.vstack((shift_x.ravel(), shift_y.ravel(),
                                  shift_x.ravel(), shift_y.ravel())).transpose()) # [32768,4]
        shifts = shifts.contiguous().type_as(scores).float()

        A = self._num_anchors # 11
        K = shifts.size(0) # 32768

        self._anchors = self._anchors.type_as(scores) # [11,4]
        # anchors = self._anchors.view(1, A, 4) + shifts.view(1, K, 4).permute(1, 0, 2).contiguous()
        anchors = self._anchors.view(1, A, 4) + shifts.view(K, 1, 4) # [32768,11,4]
        anchors = anchors.view(1, K * A, 4).expand(batch_size, K * A, 4) #[1,360448,4]

        # Transpose and reshape predicted bbox transformations to get them
        # into the same order as the anchors:

        bbox_deltas = bbox_deltas.permute(0, 2, 3, 1).contiguous() # [1,128,256,44]
        bbox_deltas = bbox_deltas.view(batch_size, -1, 4) # [1,360448,4]

        # Same story for the scores:
        scores = scores.permute(0, 2, 3, 1).contiguous() # [1,128,256,11]
        scores = scores.view(batch_size, -1) # [1,360448]

        # Convert anchors into proposals via bbox transformations
        proposals = bbox_transform_inv(anchors, bbox_deltas, batch_size) # [1,360448,4]

        # 2. clip predicted boxes to image
        proposals = clip_boxes(proposals, im_info, batch_size) # [1,360448,4]
        # proposals = clip_boxes_batch(proposals, im_info, batch_size)

        ignore_label = num_ignore.new(batch_size, anchors.size(1)).type_as(num_ignore).fill_(1)  # [1,360448]
        if num_ignore > 0:
            overlaps_ignore = bbox_overlaps_batch(proposals, ignore_region) # [1,360448,30]
            max_overlaps, argmax_overlaps = torch.max(overlaps_ignore, 2) # [1,360448]
            gt_max_overlaps, _ = torch.max(overlaps_ignore, 1) # [1,30]
            gt_max_overlaps[gt_max_overlaps == 0] = 1e-5
            keep = torch.sum(overlaps_ignore.eq(gt_max_overlaps.view(batch_size, 1, -1).expand_as(overlaps_ignore)), 2) #[1,360448]
            ignore_label[max_overlaps > 0.7] = 0
            ignore_label[keep > 0] = 0

        ignore_label = ignore_label.view(-1) # [360448] ---- tensors used as indices must be long or byte tensors
        scores = scores[:, ignore_label]
        proposals = proposals[:, ignore_label, :]

        # assign the score to 0 if it's non keep.
        # keep = self._filter_boxes(proposals, min_size * im_info[:, 2])

        # trim keep index to make it euqal over batch
        # keep_idx = torch.cat(tuple(keep_idx), 0)

        # scores_keep = scores.view(-1)[keep_idx].view(batch_size, trim_size)
        # proposals_keep = proposals.view(-1, 4)[keep_idx, :].contiguous().view(batch_size, trim_size, 4)
        
        # _, order = torch.sort(scores_keep, 1, True)

        scores_keep = scores
        proposals_keep = proposals
        _, order = torch.sort(scores_keep, 1, True) # [1,16650]

        output = scores.new(batch_size, post_nms_topN, 5).zero_() # [1,2000,5]
        for i in range(batch_size):
            # # 3. remove predicted boxes with either height or width < threshold
            # # (NOTE: convert min_size to input image scale stored in im_info[2])
            proposals_single = proposals_keep[i] # [16650,4]
            scores_single = scores_keep[i] # [16650]

            # # 4. sort all (proposal, score) pairs by score from highest to lowest
            # # 5. take top pre_nms_topN (e.g. 6000)
            order_single = order[i] # [16650]

            if pre_nms_topN > 0 and pre_nms_topN < scores_keep.numel():
                order_single = order_single[:pre_nms_topN] # [12000]

            proposals_single = proposals_single[order_single, :] # [12000,4]
            scores_single = scores_single[order_single].view(-1,1) # [12000,1]

            # 6. apply nms (e.g. threshold = 0.7)
            # 7. take after_nms_topN (e.g. 300)
            # 8. return the top proposals (-> RoIs top)

            keep_idx_i = nms(torch.cat((proposals_single, scores_single), 1), nms_thresh, force_cpu=not cfg.USE_GPU_NMS)
            keep_idx_i = keep_idx_i.long().view(-1)

            if post_nms_topN > 0:
                keep_idx_i = keep_idx_i[:post_nms_topN] # [2000]
            proposals_single = proposals_single[keep_idx_i, :] # [2000,4]
            scores_single = scores_single[keep_idx_i, :] # [2000,1]

            # padding 0 at the end.
            num_proposal = proposals_single.size(0) # 2000
            output[i,:,0] = i
            output[i,:num_proposal,1:] = proposals_single

        return output

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass

    def _filter_boxes(self, boxes, min_size):
        """Remove all boxes with any side smaller than min_size."""
        ws = boxes[:, :, 2] - boxes[:, :, 0] + 1
        hs = boxes[:, :, 3] - boxes[:, :, 1] + 1
        keep = ((ws >= min_size.view(-1,1).expand_as(ws)) & (hs >= min_size.view(-1,1).expand_as(hs)))
        return keep
