# Copyright (c) 2021 Huawei Technologies Co., Ltd.
# Licensed under CC BY-NC-SA 4.0 (Attribution-NonCommercial-ShareAlike 4.0 International) (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode
#
# The code is released for academic research use only. For commercial use, please contact Huawei Technologies Co., Ltd.
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys

env_path = os.path.join(os.path.dirname(__file__), '../..')
if env_path not in sys.path:
    sys.path.append(env_path)

from dataset.grayscale_denoise_test_set import GrayscaleDenoiseTestSet
from dataset.color_denoise_test_set import ColorDenoiseTestSet
import torch

from models.loss.image_quality_v2 import PSNR, SSIM, LPIPS, MappedLoss
from evaluation.common_utils.display_utils import generate_formatted_report
from data.postprocessing_functions import DenoisingPostProcess
import time
import argparse
import importlib
import cv2
import numpy as np
import tqdm
from admin.environment import env_settings


def compute_score(setting_name, load_saved=False, noise_level=2, mode='grayscale'):
    """ Compute scores on Denoising test set. If load_saved is true, the script will use pre-computed
        images whenever available. Otherwise, the images are generated by running the network. setting_name denotes
        the name of the experiment setting to be used. """

    expr_module = importlib.import_module('evaluation.burst_denoise.experiments.{}'.format(setting_name))
    expr_func = getattr(expr_module, 'main')
    network_list = expr_func()

    base_results_dir = env_settings().save_data_path

    if mode == 'grayscale':
        dataset = GrayscaleDenoiseTestSet(noise_level=noise_level)
    else:
        dataset = ColorDenoiseTestSet(noise_level=noise_level)

    mapping_fn = DenoisingPostProcess(return_np=False).process

    metrics = ('psnr', 'ssim', 'lpips')
    device = 'cuda'
    boundary_ignore = 8
    metrics_all = {}
    scores = {}
    for m in metrics:
        if m == 'psnr':
            loss_fn = MappedLoss(base_loss=PSNR(boundary_ignore=boundary_ignore), mapping_fn=mapping_fn)
        elif m == 'ssim':
            loss_fn = MappedLoss(base_loss=SSIM(boundary_ignore=boundary_ignore, use_for_loss=False),
                                 mapping_fn=mapping_fn)
        elif m == 'lpips':
            loss_fn = MappedLoss(base_loss=LPIPS(boundary_ignore=boundary_ignore), mapping_fn=mapping_fn)
            loss_fn.to(device)
        else:
            raise Exception
        metrics_all[m] = loss_fn
        scores[m] = []

    scores_all = {}

    for n in network_list:
        scores = {k: [] for k, v in scores.items()}

        out_dir = '{}/denoise_{}/noise_{}/{}'.format(base_results_dir, mode, noise_level, n.get_unique_name())

        using_saved_results = False
        if load_saved:
            # Check if results directory exists
            if os.path.isdir(out_dir):
                result_list = os.listdir(out_dir)
                result_list = [res for res in result_list if res[-3:] == 'png']

                # Check if number of results match
                # TODO use a better criteria
                if len(result_list) == len(dataset):
                    using_saved_results = True

        if not using_saved_results:
            net = n.load_net()
            device = 'cuda'
            net.to(device).train(False)

        for idx in tqdm.tqdm(range(len(dataset))):
            burst, gt, meta_info = dataset[idx]
            burst_name = meta_info['burst_name']

            burst = burst.to(device).unsqueeze(0)
            gt = gt.to(device)
            noise_estimate = meta_info['sigma_estimate'].to(device).unsqueeze(0)

            if n.burst_sz is not None:
                burst = burst[:, :n.burst_sz]

            if using_saved_results:
                net_pred = cv2.imread('{}/{}.png'.format(out_dir, burst_name), cv2.IMREAD_UNCHANGED)

                if mode == 'grayscale':
                    net_pred = (torch.from_numpy(net_pred.astype(np.float32)) / 2 ** 14).float().to(device).unsqueeze(0)
                else:
                    net_pred = (torch.from_numpy(net_pred.astype(np.float32)) / 2 ** 14).permute(2, 0, 1).float().to(device)
                net_pred = net_pred.unsqueeze(0)
            else:

                with torch.no_grad():
                    net_pred, _ = net(burst, noise_estimate=noise_estimate)

                # Perform quantization to be consistent with evaluating on saved images
                net_pred_int = (net_pred.clamp(0.0, 1.0) * 2 ** 14).short()
                net_pred = net_pred_int.float() / (2 ** 14)

            for m, m_fn in metrics_all.items():
                metric_value = m_fn(net_pred, gt.unsqueeze(0), meta_info=[meta_info, ]).cpu().item()
                scores[m].append(metric_value)

        scores_all[n.get_display_name()] = scores

    scores_all_mean = {net: {m: sum(s) / len(s) for m, s in scores.items()} for net, scores in scores_all.items()}

    report_text = generate_formatted_report(scores_all_mean)
    print('Mode: {} Noise level is: {}'.format(mode, noise_level))
    print(report_text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Compute scores on burst denoising test sets. If load_saved is '
                                                 'true, the script will use pre-computed images whenever available. '
                                                 'Otherwise, the images are generated by running the network. '
                                                 'setting_name denotes the name of the experiment setting to be used.')
    parser.add_argument('setting', type=str, help='Name of experiment setting')
    parser.add_argument('mode', type=str, help='grayscale or color')
    parser.add_argument('noise_level', type=str, help='Noise Level (can be 1, 2, 4, 8 or all)')
    parser.add_argument('--load_saved', dest='load_saved', action='store_true', default=False)

    args = parser.parse_args()

    if args.noise_level == 'all':
        for level in [1, 2, 4, 8]:
            compute_score(args.setting, mode=args.mode, noise_level=level, load_saved=args.load_saved)
    else:
        assert int(args.noise_level) in [1, 2, 4, 8]
        compute_score(args.setting, mode=args.mode, noise_level=int(args.noise_level), load_saved=args.load_saved)